"""
Inline keyboard layouts for Telegram bot.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def memo_approval_keyboard(
    memo_id: int, show_deep_research: bool = True, opus_recommendation: str = "proceed",
) -> InlineKeyboardMarkup:
    """Return the appropriate keyboard based on Opus recommendation."""
    if opus_recommendation in ("proceed", "reduce_size"):
        # TRADE keyboard
        rows = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{memo_id}"),
                InlineKeyboardButton("✏️ Modify", callback_data=f"modify_{memo_id}"),
            ],
            [
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{memo_id}"),
                InlineKeyboardButton("👀 Watchlist", callback_data=f"watchlist_{memo_id}"),
            ],
        ]
    elif opus_recommendation == "watchlist":
        # WATCHLIST keyboard
        rows = [
            [
                InlineKeyboardButton("👀 Add to Watchlist", callback_data=f"watchlist_{memo_id}"),
                InlineKeyboardButton("⚡ Override: Trade", callback_data=f"override_{memo_id}"),
            ],
            [
                InlineKeyboardButton("🚫 Dismiss", callback_data=f"dismiss_{memo_id}"),
            ],
        ]
    elif opus_recommendation == "pass":
        # PASS keyboard
        rows = [
            [
                InlineKeyboardButton("🚫 Dismiss", callback_data=f"dismiss_{memo_id}"),
                InlineKeyboardButton("⚡ Override: Trade", callback_data=f"override_{memo_id}"),
            ],
        ]
    else:
        # Default TRADE keyboard (backward compat)
        rows = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{memo_id}"),
                InlineKeyboardButton("✏️ Modify", callback_data=f"modify_{memo_id}"),
            ],
            [
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{memo_id}"),
                InlineKeyboardButton("👀 Watchlist", callback_data=f"watchlist_{memo_id}"),
            ],
        ]
    if show_deep_research:
        rows.append([
            InlineKeyboardButton("🔬 Run Deep Research", callback_data=f"deep_research_{memo_id}"),
        ])
    return InlineKeyboardMarkup(rows)


def modify_keyboard(memo_id: int) -> InlineKeyboardMarkup:
    """Sub-menu for modifying trade parameters."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Entry Price", callback_data=f"mod_entry_{memo_id}"),
            InlineKeyboardButton("Position Size", callback_data=f"mod_size_{memo_id}"),
        ],
        [
            InlineKeyboardButton("Stop-Loss", callback_data=f"mod_stop_{memo_id}"),
            InlineKeyboardButton("Targets", callback_data=f"mod_target_{memo_id}"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"back_{memo_id}"),
        ],
    ])


def confirm_keyboard(action: str, memo_id: int) -> InlineKeyboardMarkup:
    """Confirmation for trade actions."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{action}_{memo_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{memo_id}"),
        ],
    ])


def close_confirm_keyboard(ticker: str) -> InlineKeyboardMarkup:
    """Confirmation for closing a position."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Close Position", callback_data=f"close_confirm_{ticker}"),
            InlineKeyboardButton("❌ Cancel", callback_data="close_cancel"),
        ],
    ])


# ── Position Monitor Keyboards ──

def position_stop_keyboard(ticker: str, trade_id: int) -> InlineKeyboardMarkup:
    """Keyboard for stop-loss breach alert."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Close Now ✅", callback_data=f"pos_close_{ticker}"),
            InlineKeyboardButton("Hold (Override) ⚡", callback_data=f"pos_hold_{trade_id}"),
        ],
    ])


def position_target_keyboard(ticker: str, trade_id: int, target_num: int) -> InlineKeyboardMarkup:
    """Keyboard for target approaching alert."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Sell 50% at T{target_num} ✅", callback_data=f"pos_sell50_{ticker}"),
            InlineKeyboardButton("Hold ✅", callback_data=f"pos_hold_{trade_id}"),
        ],
        [
            InlineKeyboardButton("Close All Now 🔴", callback_data=f"pos_close_{ticker}"),
        ],
    ])


def position_target_hit_keyboard(ticker: str, trade_id: int, target_num: int) -> InlineKeyboardMarkup:
    """Keyboard for target hit alert."""
    if target_num == 1:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Sell 50% + Move Stop ✅", callback_data=f"pos_t1exit_{ticker}"),
                InlineKeyboardButton("Sell 50% Only ✅", callback_data=f"pos_sell50_{ticker}"),
            ],
            [
                InlineKeyboardButton("Hold All ✅", callback_data=f"pos_hold_{trade_id}"),
                InlineKeyboardButton("Close All 🔴", callback_data=f"pos_close_{ticker}"),
            ],
        ])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Close All ✅", callback_data=f"pos_close_{ticker}"),
            InlineKeyboardButton("Hold ✅", callback_data=f"pos_hold_{trade_id}"),
        ],
    ])


def position_time_keyboard(ticker: str, trade_id: int) -> InlineKeyboardMarkup:
    """Keyboard for time expiring alert."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Close Now ✅", callback_data=f"pos_close_{ticker}"),
            InlineKeyboardButton("Extend 5 Days ✅", callback_data=f"pos_extend_{trade_id}"),
        ],
        [
            InlineKeyboardButton("Let Expire ✅", callback_data=f"pos_hold_{trade_id}"),
        ],
    ])


def position_time_expired_keyboard(ticker: str, trade_id: int) -> InlineKeyboardMarkup:
    """Keyboard for time expired alert."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm Close ✅", callback_data=f"pos_close_{ticker}"),
            InlineKeyboardButton("Override: Extend ⚡", callback_data=f"pos_extend_{trade_id}"),
        ],
    ])


def position_giveback_keyboard(ticker: str, trade_id: int) -> InlineKeyboardMarkup:
    """Keyboard for profit giveback alert."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Sell 50% ✅", callback_data=f"pos_sell50_{ticker}"),
            InlineKeyboardButton("Hold ✅", callback_data=f"pos_hold_{trade_id}"),
        ],
    ])
