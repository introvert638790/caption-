"""
Help: /help command + "❓ Help" menu button, and all guide sub-screens.

Purely informational -- displays static text explaining bot purpose and
feature usage. Does not touch caption editing, post deletion, or any other
core logic; reads no database state and calls no core/ functions.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import keyboards

router = Router(name="help")


ABOUT_TEXT = (
    "📖 About This Bot\n\n"
    "📋 Caption Manager Bot\n\n"
    "This bot helps you bulk-edit captions across your\n"
    "Telegram channel — without doing it one message\n"
    "at a time.\n\n"
    "📂 Caption Manager — edit captions in bulk\n\n"
    "Only one task runs at a time. You can pause,\n"
    "resume, or stop anytime from Job Status."
)

CAPTION_MANAGER_TEXT = (
    "📂 Caption Manager Guide — Basics\n\n"
    "Caption Manager edits captions on posts already\n"
    "in your channel — in bulk, across a range you pick.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🛠 Setup (do this first)\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "1️⃣ 📡 Configure Channel\n"
    "   Forward any post from your channel to the bot.\n"
    "   Bot must be admin there with Edit Message rights.\n\n"
    "2️⃣ 🎯 Set Processing Range\n"
    "   Forward the first post, then the last post of\n"
    "   the range you want to process.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🧩 Core Features\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "🔤 Find & Replace\n"
    "   Finds a word or multi-word phrase in captions\n"
    "   and replaces it with another — whole-word match,\n"
    "   case-insensitive.\n"
    "   → Find word: single word or full phrase. If it\n"
    "     was clickable (hyperlinked) in the original\n"
    "     caption, that link is removed along with it.\n"
    "   → Replace word: can also be multi-word, and part\n"
    "     of it can be set as a clickable hyperlink —\n"
    "     e.g. only \"ALEX\" linked inside \"ALEX is King 👑\".\n\n"
    "🧹 Caption Cleanup — 3 independent switches\n"
    "   • Remove Direct URLs\n"
    "     Deletes plain-text links like https://...,\n"
    "     www...., t.me/..., telegram.me/...\n"
    "   • Remove Hyperlink Formatting\n"
    "     Removes Telegram hyperlinks but keeps the\n"
    "     visible text underneath.\n"
    "   • Quote Removal\n"
    "     Strips blockquote formatting only — the text\n"
    "     itself is always preserved.\n"
    "   → Mix and match any combination of the three.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "✅ Enable ≠ Saved\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Saving a value only stores it. Nothing runs until\n"
    "you explicitly tap Enable on that feature's screen.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "▶️ Run It\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Tap ▶️ Preview & Run from the Main Menu. It scans\n"
    "once, shows what will change, then you confirm to\n"
    "start. Pause/Resume/Stop anytime from Job Status.\n\n"
    "Looking for Caption Injector or Add Hyperlink?\n"
    "→ See the Advanced guide."
)

CAPTION_MANAGER_ADVANCED_TEXT = (
    "🧩 Caption Manager Guide — Advanced\n\n"
    "Power-user features that go beyond basic\n"
    "find-and-replace or cleanup.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "💉 Caption Injector\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Adds your own text to the BOTTOM of every caption,\n"
    "automatically.\n"
    "→ Set the text once, then tap Enable. Turning it\n"
    "off later keeps your saved text for next time.\n"
    "→ Supports multi-word text, and part of it can be\n"
    "   set as a clickable hyperlink — same idea as\n"
    "   Find & Replace, e.g. only \"ALEX\" linked inside\n"
    "   \"ALEX is King 👑\".\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🔗 Add Hyperlink\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Makes the entire caption clickable, linking to one\n"
    "URL you set.\n"
    "→ Applies to the whole caption text at once.\n"
    "→ Any existing links in the caption are replaced.\n"
    "→ Quote formatting (if Quote Removal is off) is\n"
    "   preserved — the link wraps around it cleanly.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🔀 Processing Order\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "When multiple features are enabled together, they\n"
    "always apply in this fixed order:\n\n"
    "Remove URLs → Remove Hyperlinks → Quote Removal\n"
    "→ Promotional Line Remover → Find & Replace\n"
    "→ Caption Injector → Add Hyperlink\n\n"
    "Add Hyperlink always runs last, so it wraps the\n"
    "final version of your caption — after every other\n"
    "edit has already been applied."
)

KEEP_ALIVE_SETTINGS_TEXT = (
    "🟢 Keep Alive & ⚙️ Settings\n\n"
    "🟢 Keep Alive\n"
    "🔄 Ping Now — sends a quick test ping\n"
    "📊 Status — shows the last ping result\n\n"
    "⚙️ Settings\n"
    "⏱️ Delay — wait time between messages (1.0–3.0s)\n\n"
    "💬 Commands\n"
    "/cancel — stop whatever you're setting up right now\n"
    "and return to the Main Menu"
)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "❓ Help",
        reply_markup=keyboards.help_menu(),
    )


@router.callback_query(F.data == "menu:help")
async def cb_open_help(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "❓ Help",
        reply_markup=keyboards.help_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "help:about")
async def cb_help_about(callback: CallbackQuery) -> None:
    await callback.message.edit_text(ABOUT_TEXT, reply_markup=keyboards.back_to_help())
    await callback.answer()


@router.callback_query(F.data == "help:caption_manager")
async def cb_help_caption_manager(callback: CallbackQuery) -> None:
    await callback.message.edit_text(CAPTION_MANAGER_TEXT, reply_markup=keyboards.back_to_help())
    await callback.answer()


@router.callback_query(F.data == "help:caption_manager_advanced")
async def cb_help_caption_manager_advanced(callback: CallbackQuery) -> None:
    await callback.message.edit_text(CAPTION_MANAGER_ADVANCED_TEXT, reply_markup=keyboards.back_to_help())
    await callback.answer()


@router.callback_query(F.data == "help:keep_alive_settings")
async def cb_help_keep_alive_settings(callback: CallbackQuery) -> None:
    await callback.message.edit_text(KEEP_ALIVE_SETTINGS_TEXT, reply_markup=keyboards.back_to_help())
    await callback.answer()
