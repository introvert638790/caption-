"""
Centralized inline keyboard builders.

All button markup lives here so handlers stay focused on flow logic. Every
callback_data string is namespaced with a short prefix (e.g. "menu:", "job:")
to keep routing in dispatcher.py unambiguous.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    """Root menu shown by /start."""
    buttons = [
        [InlineKeyboardButton(text="📡 Configure Channel", callback_data="menu:set_channel")],
        [InlineKeyboardButton(text="📂 Caption Manager", callback_data="menu:caption_manager")],
        [InlineKeyboardButton(text="🎯 Set Processing Range", callback_data="menu:set_range")],
        [InlineKeyboardButton(text="▶️ Preview & Run", callback_data="menu:preview")],
        [InlineKeyboardButton(text="📊 Job Status", callback_data="menu:status")],
        [InlineKeyboardButton(text="🟢 Keep Alive", callback_data="menu:keep_alive")],
        [InlineKeyboardButton(text="⚙️ Settings", callback_data="menu:settings")],
        [InlineKeyboardButton(text="❓ Help", callback_data="menu:help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def keep_alive_menu() -> InlineKeyboardMarkup:
    """Keep Alive submenu."""
    buttons = [
        [InlineKeyboardButton(text="🔄 Ping Now", callback_data="ka:ping")],
        [InlineKeyboardButton(text="⚙️ Keep Alive Settings", callback_data="ka:settings")],
        [InlineKeyboardButton(text="📊 Status", callback_data="ka:status")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Mode -> (display label with icon, callback suffix)
KEEP_ALIVE_MODE_LABELS: dict[str, str] = {
    "manual": "🖐 Manual",
    "auto": "🤖 Auto",
    "task_protection": "🛡 Task Protection",
}

# Interval presets in seconds -> display label.
KEEP_ALIVE_INTERVAL_LABELS: dict[int, str] = {
    300: "5 min",
    420: "7 min",
    540: "9 min",
    600: "10 min",
    720: "12 min",
    900: "15 min",
}


def keep_alive_settings_menu(current_mode: str, current_interval_seconds: int) -> InlineKeyboardMarkup:
    """
    Keep Alive Settings screen: mode selection + interval presets.
    Currently selected option is marked with a leading checkmark.
    """
    mode_buttons = []
    for mode, label in KEEP_ALIVE_MODE_LABELS.items():
        text = f"✅ {label}" if mode == current_mode else label
        mode_buttons.append([InlineKeyboardButton(text=text, callback_data=f"ka:mode:{mode}")])

    interval_items = list(KEEP_ALIVE_INTERVAL_LABELS.items())
    interval_row_buttons = [
        InlineKeyboardButton(
            text=f"✅ {label}" if seconds == current_interval_seconds else label,
            callback_data=f"ka:interval:{seconds}",
        )
        for seconds, label in interval_items
    ]
    interval_rows = [interval_row_buttons[i:i + 3] for i in range(0, len(interval_row_buttons), 3)]

    buttons = mode_buttons + interval_rows
    buttons.append([InlineKeyboardButton(text="⬅️ Back to Keep Alive", callback_data="menu:keep_alive")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def caption_manager_menu() -> InlineKeyboardMarkup:
    """Caption Manager submenu: configuration-only entry points for each feature."""
    buttons = [
        [InlineKeyboardButton(text="🔤 Find & Replace", callback_data="cm:find_replace")],
        [InlineKeyboardButton(text="🧹 Caption Cleanup", callback_data="cm:cleanup")],
        [InlineKeyboardButton(text="🚫 Promotional Line Remover", callback_data="cm:promo_remover")],
        [InlineKeyboardButton(text="💉 Caption Injector", callback_data="cm:injector")],
        [InlineKeyboardButton(text="🔗 Add Hyperlink", callback_data="cm:add_hyperlink")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def feature_toggle_menu(
    feature_prefix: str,
    enabled: bool,
    extra_buttons: list[list[InlineKeyboardButton]] | None = None,
    back_to: str = "main_menu",
) -> InlineKeyboardMarkup:
    """
    Shared Enable/Disable + extra action buttons for a Caption Manager
    feature config screen (Find & Replace, Caption Cleanup, Caption
    Injector, Promotional Line Remover). `feature_prefix` namespaces the
    callback_data (e.g. "fr" for Find & Replace, "inj" for Injector,
    "promo" for Promotional Line Remover).

    `back_to` controls the final navigation button: "caption_manager"
    returns to the Caption Manager submenu (used by all Caption Manager
    feature screens); "main_menu" (default) preserves original behavior
    for any caller not updated to the new navigation.
    """
    toggle_button = (
        InlineKeyboardButton(text="🔴 Disable", callback_data=f"{feature_prefix}:disable")
        if enabled
        else InlineKeyboardButton(text="🟢 Enable", callback_data=f"{feature_prefix}:enable")
    )
    buttons = [[toggle_button]]
    if extra_buttons:
        buttons.extend(extra_buttons)

    if back_to == "caption_manager":
        buttons.append([InlineKeyboardButton(text="⬅️ Back to Caption Manager", callback_data="menu:caption_manager")])
    else:
        buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:root")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_menu() -> InlineKeyboardMarkup:
    """Single "back" button, used after a setup step completes."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="menu:root")]]
    )


def main_menu_button() -> InlineKeyboardMarkup:
    """
    Single reusable "🏠 Main Menu" button, attached to every setup/waiting
    screen (Configure Channel, Range, Words, etc.) so the user is never
    forced to type /start to escape a flow.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:root")]]
    )


def help_menu() -> InlineKeyboardMarkup:
    """Help index screen -- lists each guide, plus Main Menu (this is the only help screen with Main Menu, not Back to Help)."""
    buttons = [
        [InlineKeyboardButton(text="📖 About This Bot", callback_data="help:about")],
        [InlineKeyboardButton(text="🔤 Caption Manager Guide — Basics", callback_data="help:caption_manager")],
        [InlineKeyboardButton(text="🧩 Caption Manager Guide — Advanced", callback_data="help:caption_manager_advanced")],
        [InlineKeyboardButton(text="🟢 Keep Alive & Settings Guide", callback_data="help:keep_alive_settings")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_help() -> InlineKeyboardMarkup:
    """Single reusable "Back to Help" button for every Help sub-guide screen."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Help", callback_data="menu:help")]]
    )


def back_to_caption_manager() -> InlineKeyboardMarkup:
    """Single reusable "Back to Caption Manager" button for Caption Manager sub-screens and setup flows."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Caption Manager", callback_data="menu:caption_manager")]]
    )


def confirm_channel(chat_id: int, title: str) -> InlineKeyboardMarkup:
    """Shown after a forwarded post is detected, asking to confirm the channel."""
    buttons = [
        [InlineKeyboardButton(text=f"✅ Use \"{title}\"", callback_data=f"channel:confirm:{chat_id}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_run(preview_count: int) -> InlineKeyboardMarkup:
    """Preview confirmation gate before a real run starts (dry-run result)."""
    buttons = [
        [InlineKeyboardButton(text=f"▶️ Start Run ({preview_count} messages)", callback_data="job:start")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="menu:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cached_preview_actions(preview_count: int) -> InlineKeyboardMarkup:
    """
    Shown when a valid cached preview exists (same channel/range/words as
    last scan, and no job has completed since) -- offers reusing it without
    rescanning, or forcing a fresh scan.
    """
    buttons = [
        [InlineKeyboardButton(text=f"▶️ Start Run ({preview_count} messages)", callback_data="job:start")],
        [InlineKeyboardButton(text="🔄 Rescan", callback_data="menu:preview:rescan")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def job_controls(is_paused: bool = False) -> InlineKeyboardMarkup:
    """Shown while a job is active, or paused and resumable."""
    if is_paused:
        buttons = [
            [InlineKeyboardButton(text="▶️ Resume", callback_data="job:resume")],
            [InlineKeyboardButton(text="⏹️ Stop Permanently", callback_data="job:stop")],
        ]
    else:
        buttons = [
            [InlineKeyboardButton(text="⏸️ Pause", callback_data="job:pause")],
            [InlineKeyboardButton(text="⏹️ Stop", callback_data="job:stop")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_stop() -> InlineKeyboardMarkup:
    """Safety confirmation before permanently stopping a job."""
    buttons = [
        [InlineKeyboardButton(text="⚠️ Yes, stop permanently", callback_data="job:stop:confirmed")],
        [InlineKeyboardButton(text="⬅️ Cancel", callback_data="menu:status")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)





def settings_menu() -> InlineKeyboardMarkup:
    """Settings submenu."""
    buttons = [
        [InlineKeyboardButton(text="⏱️ Delay", callback_data="settings:delay")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:root")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
