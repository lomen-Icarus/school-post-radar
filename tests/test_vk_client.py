import httpx

from radar.vk.client import VkClient
from radar.vk.models import parse_post
from tests.conftest import make_post


async def test_execute_batch_maps_errors_in_order(fake_vk):
    fake_vk.walls[-1] = [make_post(-1, 10, "Ура, победа!")]
    fake_vk.walls[-2] = {"error_code": 15, "error_msg": "Access denied"}
    fake_vk.walls[-3] = [
        make_post(-3, 11, "Пост", copy_history=[{"text": "репост", "attachments": [{"type": "photo"}]}])
    ]
    client = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()), use_execute=True)
    res = await client.wall_get_many([-1, -2, -3], count=5, batch_size=25)
    assert res[-1].posts[0].post_id == 10 and res[-1].error is None
    assert res[-2].error is not None and res[-2].error.code == 15 and res[-2].error.is_permanent
    assert res[-3].posts[0].is_repost and res[-3].posts[0].attachments == ["photo"]
    assert "[Репост]" in res[-3].posts[0].text
    assert client.execute_supported is True
    assert sum(1 for m, _ in fake_vk.calls if m == "execute") == 1
    await client.close()


async def test_fallback_to_single_calls_when_execute_unavailable(fake_vk):
    fake_vk.fail_execute = True
    fake_vk.walls[-1] = [make_post(-1, 1, "a")]
    fake_vk.walls[-2] = [make_post(-2, 2, "b")]
    client = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()), use_execute=True)
    res = await client.wall_get_many([-1, -2], batch_size=25)
    assert client.execute_supported is False
    assert res[-1].posts[0].post_id == 1 and res[-2].posts[0].post_id == 2
    methods = [m for m, _ in fake_vk.calls]
    assert methods == ["execute", "wall.get", "wall.get"]
    await client.close()


async def test_retry_on_error_6(fake_vk):
    fake_vk.error_6_once = True
    fake_vk.walls[-1] = [make_post(-1, 1, "a")]
    client = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()))
    res = await client.wall_get(-1)
    assert res.error is None and res.posts
    assert [m for m, _ in fake_vk.calls] == ["wall.get", "wall.get"]
    await client.close()


async def test_groups_get_by_id_both_shapes(fake_vk):
    fake_vk.groups["andrschool"] = {"id": 111, "screen_name": "andrschool", "name": "Школа", "is_closed": 0}
    client = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()))
    infos = await client.groups_get_by_id(["andrschool"])
    assert infos[0].group_id == 111 and infos[0].requested == "andrschool"

    def old_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": [{"id": 5, "screen_name": "club5", "name": "X"}]})

    client2 = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=httpx.MockTransport(old_shape)))
    infos2 = await client2.groups_get_by_id(["club5"])
    assert infos2[0].group_id == 5
    await client.close()
    await client2.close()


async def test_token_sent_as_bearer_header(fake_vk):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"response": {"count": 0, "items": []}})

    client = VkClient(
        "secret-token", rps=1000, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await client.wall_get(-1)
    assert seen["auth"] == "Bearer secret-token"
    assert "secret-token" not in seen["body"] and "v=5.199" in seen["body"]
    await client.close()


async def test_check_token_report(fake_vk):
    fake_vk.fail_execute = True
    fake_vk.walls[-10812563] = [make_post(-10812563, 1, "a")]
    fake_vk.groups["10812563"] = {"id": 10812563, "screen_name": "club10812563", "name": "Гимназия"}
    client = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()), use_execute=True)
    report = await client.check_token(-10812563)
    assert (
        report["wall.get"] == "ok"
        and report["groups.getById"] == "ok"
        and report["execute"].startswith("ошибка 28")
    )
    await client.close()


async def test_service_token_mode_never_calls_execute(fake_vk):
    fake_vk.walls[-1] = [make_post(-1, 1, "a")]
    fake_vk.walls[-2] = [make_post(-2, 2, "b")]
    client = VkClient("t", rps=1000, http=httpx.AsyncClient(transport=fake_vk.transport()))
    res = await client.wall_get_many([-1, -2], batch_size=25)
    assert [m for m, _ in fake_vk.calls] == ["wall.get", "wall.get"]
    assert res[-1].posts and res[-2].posts
    await client.close()


def test_parse_post_fields():
    post = parse_post(
        {
            "id": 3,
            "owner_id": -9,
            "date": 1_700_000_000,
            "text": "Текст",
            "is_pinned": 1,
            "marked_as_ads": 0,
            "attachments": [
                {"type": "link", "link": {"url": "https://x"}},
                {"type": "doc", "doc": {"title": "Приказ"}},
            ],
        }
    )
    assert post.url == "https://vk.com/wall-9_3" and post.is_pinned and not post.is_repost
    assert post.attachments == ["link:https://x", "doc:Приказ"]
    assert len(post.content_hash) == 32
