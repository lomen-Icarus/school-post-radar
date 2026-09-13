from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from radar.bot import keyboards, texts
from radar.bot.callbacks import MenuCb, RegionCb
from radar.bot.handlers.common import edit_or_send
from radar.db import repo
from radar.db.connection import Database

router = Router(name="regions")


async def _render(event: Message | CallbackQuery, db: Database, profile_id: str) -> None:
    regions = await repo.list_regions(db, profile_id)
    enabled = sum(1 for r in regions if r.enabled)
    await edit_or_send(event, texts.regions_screen(enabled, len(regions)), keyboards.regions_menu(regions))


@router.message(Command("regions"))
async def cmd_regions(message: Message, db: Database, subscriber: repo.Subscriber) -> None:
    await _render(message, db, subscriber.profile_id)


@router.callback_query(MenuCb.filter(F.section == "regions"))
async def cb_regions(query: CallbackQuery, db: Database, subscriber: repo.Subscriber) -> None:
    await _render(query, db, subscriber.profile_id)
    await query.answer()


@router.callback_query(RegionCb.filter(F.action == "toggle"))
async def cb_toggle(
    query: CallbackQuery, callback_data: RegionCb, db: Database, subscriber: repo.Subscriber
) -> None:
    regions = {r.municipality_id: r for r in await repo.list_regions(db, subscriber.profile_id)}
    region = regions.get(callback_data.value)
    if region is None:
        await query.answer("Неизвестная территория", show_alert=True)
        return
    await repo.set_region_enabled(db, subscriber.profile_id, region.municipality_id, not region.enabled)
    await _render(query, db, subscriber.profile_id)
    await query.answer(f"{'Включено' if not region.enabled else 'Выключено'}: {region.short_name}")


@router.callback_query(RegionCb.filter(F.action.in_({"all", "none"})))
async def cb_all(
    query: CallbackQuery, callback_data: RegionCb, db: Database, subscriber: repo.Subscriber
) -> None:
    await repo.set_all_regions(db, subscriber.profile_id, callback_data.action == "all")
    await _render(query, db, subscriber.profile_id)
    await query.answer(
        "Все территории включены" if callback_data.action == "all" else "Все территории выключены"
    )
