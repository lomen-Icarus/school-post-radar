from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from radar.bot.callbacks import FeedbackCb, NoopCb
from radar.bot.keyboards import card_keyboard
from radar.db import repo
from radar.db.connection import Database

router = Router(name="feedback")


@router.callback_query(FeedbackCb.filter())
async def cb_feedback(query: CallbackQuery, callback_data: FeedbackCb, db: Database) -> None:
    candidate = await repo.get_candidate(db, callback_data.cid)
    if candidate is None:
        await query.answer("Запись не найдена", show_alert=True)
        return
    user_id = query.from_user.id
    if callback_data.action == "reject":
        await repo.set_candidate_review(db, candidate.candidate_id, "rejected", user_id)
        markup = card_keyboard(candidate.candidate_id, candidate.url, rejected=True)
        toast = "Отмечено как «не достижение» — больше никому не покажем"
    elif callback_data.action == "done":
        await repo.set_candidate_review(db, candidate.candidate_id, "done", user_id)
        markup = card_keyboard(candidate.candidate_id, candidate.url, done=True)
        toast = "Отлично! Отмечено как поздравленное"
    else:  # undo
        await repo.set_candidate_review(db, candidate.candidate_id, "pending", user_id)
        markup = card_keyboard(candidate.candidate_id, candidate.url)
        toast = "Возвращено"
    if isinstance(query.message, Message):
        try:
            await query.message.edit_reply_markup(reply_markup=markup)
        except Exception:  # сообщение могло быть удалено — не критично
            pass
    await query.answer(toast)


@router.callback_query(NoopCb.filter())
async def cb_noop(query: CallbackQuery) -> None:
    await query.answer()
