from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from radar.utils.timeutil import from_unix


@dataclass
class VkPost:
    owner_id: int
    post_id: int
    date: datetime
    text: str
    own_text: str
    repost_text: str
    is_repost: bool
    is_pinned: bool
    marked_as_ads: bool
    post_type: str
    attachments: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        return f"https://vk.com/wall{self.owner_id}_{self.post_id}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:32]


def _attachment_summary(items: list[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    for att in items or []:
        kind = att.get("type")
        if not kind:
            continue
        if kind == "link":
            url = (att.get("link") or {}).get("url")
            out.append(f"link:{url}" if url else "link")
        elif kind == "doc":
            title = (att.get("doc") or {}).get("title")
            out.append(f"doc:{title}" if title else "doc")
        else:
            out.append(str(kind))
    return out


def parse_post(item: dict[str, Any]) -> VkPost:
    """Преобразовать объект поста wall.get (v5.199) в VkPost. Текст репоста добавляется к тексту."""
    own_text = (item.get("text") or "").strip()
    repost_parts: list[str] = []
    attachments = _attachment_summary(item.get("attachments"))
    for copy in item.get("copy_history") or []:
        t = (copy.get("text") or "").strip()
        if t:
            repost_parts.append(t)
        attachments.extend(_attachment_summary(copy.get("attachments")))
    repost_text = "\n\n".join(repost_parts)
    is_repost = bool(item.get("copy_history"))
    if own_text and repost_text:
        text = f"{own_text}\n\n[Репост]\n{repost_text}"
    else:
        text = own_text or repost_text
    return VkPost(
        owner_id=int(item.get("owner_id") or item.get("to_id") or 0),
        post_id=int(item["id"]),
        date=from_unix(int(item.get("date") or 0)),
        text=text,
        own_text=own_text,
        repost_text=repost_text,
        is_repost=is_repost,
        is_pinned=bool(item.get("is_pinned")),
        marked_as_ads=bool(item.get("marked_as_ads")),
        post_type=str(item.get("post_type") or "post"),
        attachments=attachments,
        raw=item,
    )


def dumps_compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
