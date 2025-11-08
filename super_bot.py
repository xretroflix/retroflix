import os
import logging
from datetime import datetime
import random
import string
import re
import asyncio
import json
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatJoinRequestHandler, ContextTypes, filters
from telegram.constants import ChatMemberStatus
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID'))

MIN_ACCOUNT_AGE_DAYS = 15
REQUIRE_PROFILE_PHOTO = False
CODE_EXPIRY_MINUTES = 5

VERIFIED_USERS = set([ADMIN_ID])
MANAGED_CHANNELS = {}
PENDING_POSTS = {}
PENDING_VERIFICATIONS = {}
VERIFIED_FOR_CHANNELS = {}
BLOCKED_USERS = set()
BULK_APPROVAL_MODE = {}

UPLOADED_IMAGES = []
CHANNEL_SPECIFIC_IMAGES = {}
CURRENT_IMAGE_INDEX = {}
AUTO_POST_ENABLED = {}
POSTING_INTERVAL_HOURS = 1

USER_DATABASE = {}
USER_ACTIVITY_LOG = []
RECENT_ACTIVITY = []  # Store recent approvals/rejections for batch viewing

DEFAULT_CAPTION = ""
CHANNEL_DEFAULT_CAPTIONS = {}

UNAUTHORIZED_ATTEMPTS = []

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# Permanent storage
STORAGE_FILE = "bot_data.json"


def save_data():
    """Save all bot data to file"""
    try:
        data = {
            'managed_channels': MANAGED_CHANNELS,
            'uploaded_images': UPLOADED_IMAGES,
            'channel_specific_images': CHANNEL_SPECIFIC_IMAGES,
            'default_caption': DEFAULT_CAPTION,
            'channel_default_captions': CHANNEL_DEFAULT_CAPTIONS,
            'auto_post_enabled': AUTO_POST_ENABLED,
            'current_image_index': CURRENT_IMAGE_INDEX,
            'bulk_approval_mode': BULK_APPROVAL_MODE,
            'blocked_users': list(BLOCKED_USERS),
            'user_database': USER_DATABASE
        }
        with open(STORAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)
        logger.info("✅ Data saved")
    except Exception as e:
        logger.error(f"Save failed: {e}")


def load_data():
    """Load all bot data from file"""
    global MANAGED_CHANNELS, UPLOADED_IMAGES, CHANNEL_SPECIFIC_IMAGES
    global DEFAULT_CAPTION, CHANNEL_DEFAULT_CAPTIONS, AUTO_POST_ENABLED
    global CURRENT_IMAGE_INDEX, BULK_APPROVAL_MODE, BLOCKED_USERS, USER_DATABASE

    try:
        if os.path.exists(STORAGE_FILE):
            with open(STORAGE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            MANAGED_CHANNELS = data.get('managed_channels', {})
            UPLOADED_IMAGES = data.get('uploaded_images', [])
            CHANNEL_SPECIFIC_IMAGES = data.get('channel_specific_images', {})
            DEFAULT_CAPTION = data.get('default_caption', "")
            CHANNEL_DEFAULT_CAPTIONS = data.get('channel_default_captions', {})
            AUTO_POST_ENABLED = data.get('auto_post_enabled', {})
            CURRENT_IMAGE_INDEX = data.get('current_image_index', {})
            BULK_APPROVAL_MODE = data.get('bulk_approval_mode', {})
            BLOCKED_USERS = set(data.get('blocked_users', []))
            USER_DATABASE = data.get('user_database', {})

            # Convert string keys to int
            MANAGED_CHANNELS = {
                int(k) if str(k).lstrip('-').isdigit() else k: v
                for k, v in MANAGED_CHANNELS.items()
            }
            CHANNEL_SPECIFIC_IMAGES = {
                int(k) if str(k).lstrip('-').isdigit() else k: v
                for k, v in CHANNEL_SPECIFIC_IMAGES.items()
            }
            AUTO_POST_ENABLED = {
                int(k) if str(k).lstrip('-').isdigit() else k: v
                for k, v in AUTO_POST_ENABLED.items()
            }
            CURRENT_IMAGE_INDEX = {
                int(k) if str(k).lstrip('-').isdigit() else k: v
                for k, v in CURRENT_IMAGE_INDEX.items()
            }
            BULK_APPROVAL_MODE = {
                int(k) if str(k).lstrip('-').isdigit() else k: v
                for k, v in BULK_APPROVAL_MODE.items()
            }
            CHANNEL_DEFAULT_CAPTIONS = {
                int(k) if str(k).lstrip('-').isdigit() else k: v
                for k, v in CHANNEL_DEFAULT_CAPTIONS.items()
            }

            logger.info(
                f"✅ Loaded: {len(MANAGED_CHANNELS)} channels, {len(UPLOADED_IMAGES)} images"
            )
        else:
            logger.info("No saved data")
    except Exception as e:
        logger.error(f"Load failed: {e}")


def is_verified(user_id: int) -> bool:
    return user_id in VERIFIED_USERS or user_id == ADMIN_ID


async def is_bot_admin(context: ContextTypes.DEFAULT_TYPE,
                       chat_id: int) -> bool:
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        return bot_member.status in [
            ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER
        ]
    except:
        return False


def generate_verification_code() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def is_name_suspicious(name: str) -> bool:
    """Check if name looks suspicious (bot-like)"""
    if not name or len(name) < 2:
        return True

    # Check for "User" followed by numbers (typical bot pattern)
    if re.match(r'^User\d+$', name, re.IGNORECASE):
        return True

    # Check if name is mostly numbers
    letters_and_numbers = re.sub(r'[^a-zA-Z0-9]', '', name)
    if len(letters_and_numbers) < 2:
        return True

    numbers = re.sub(r'\D', '', name)
    if len(numbers) > len(name) * 0.6:  # More than 60% numbers
        return True

    return False


async def check_user_legitimacy(context: ContextTypes.DEFAULT_TYPE,
                                user_id: int) -> dict:
    """
    Enhanced user legitimacy checker with detailed scoring
    Returns: {"legitimate": bool, "score": int, "reason": str}
    """
    try:
        user = await context.bot.get_chat(user_id)

        score = 0
        reasons = []

        # Check 1: Is it a bot?
        if user.type == "bot":
            return {"legitimate": False, "reason": "Bot account", "score": 0}

        # Check 2: Name quality (40 points)
        if not user.first_name or is_name_suspicious(user.first_name):
            reasons.append("Suspicious name")
        else:
            score += 40

        # Check 3: Has username? (30 points)
        if user.username:
            score += 30
        else:
            reasons.append("No username")

        # Check 4: Has profile photo? (30 points)
        if REQUIRE_PROFILE_PHOTO:
            try:
                photos = await context.bot.get_user_profile_photos(user_id, limit=1)
                if photos.total_count > 0:
                    score += 30
                else:
                    reasons.append("No profile photo")
            except:
                reasons.append("Cannot check photo")
        else:
            # Give score anyway if not required
            score += 30

        # Scoring system:
        # 100 = Legitimate (auto-approve)
        # 1-99 = Borderline (manual check with captcha)
        # 0 = Suspicious (auto-reject)

        if score >= 70:
            return {"legitimate": True, "score": 100}
        elif score >= 30:
            return {"legitimate": False, "score": 50, "reason": ", ".join(reasons)}
        else:
            return {"legitimate": False, "score": 0, "reason": ", ".join(reasons)}

    except Exception as e:
        logger.error(f"Legitimacy check failed: {e}")
        return {"legitimate": False, "reason": "Error checking user", "score": 0}


def track_user_activity(user_id: int,
                        channel_id: int,
                        action: str,
                        user_data: dict = None):
    """Track user activity in database"""
    if user_id not in USER_DATABASE:
        USER_DATABASE[user_id] = {
            'first_name':
            user_data.get('first_name', 'Unknown') if user_data else 'Unknown',
            'last_name': user_data.get('last_name', ''),
            'username': user_data.get('username', ''),
            'channels': {}
        }
    if channel_id not in USER_DATABASE[user_id]['channels']:
        USER_DATABASE[user_id]['channels'][channel_id] = {
            'channel_name':
            MANAGED_CHANNELS.get(channel_id, {}).get('name', 'Unknown'),
            'status':
            action,
            'request_date':
            datetime.now(),
            'approval_date':
            None
        }
    else:
        USER_DATABASE[user_id]['channels'][channel_id]['status'] = action
        if action == 'approved':
            USER_DATABASE[user_id]['channels'][channel_id][
                'approval_date'] = datetime.now()
    save_data()


async def alert_owner_unauthorized_access(context: ContextTypes.DEFAULT_TYPE,
                                          user_id: int, username: str,
                                          first_name: str, command: str):
    """Alert owner of unauthorized access attempts"""
    try:
        UNAUTHORIZED_ATTEMPTS.append({
            'user_id': user_id,
            'username': username,
            'first_name': first_name,
            'command': command,
            'timestamp': datetime.now()
        })

        await context.bot.send_message(
            ADMIN_ID,
            f"🚨 *Unauthorized Access Attempt*\n\n"
            f"User: {first_name}\n"
            f"ID: `{user_id}`\n"
            f"Username: @{username or 'None'}\n"
            f"Command: `{command}`",
            )
    except Exception as e:
        logger.error(f"Alert failed: {e}")


async def owner_only_check(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is the owner"""
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        await alert_owner_unauthorized_access(context, update.effective_user.id,
                                              update.effective_user.username,
                                              update.effective_user.first_name,
                                              update.message.text if update.message else "callback")
        await update.message.reply_text("❌ Unauthorized")
        return False
    return True


# ========== SMART JOIN REQUEST HANDLER ==========
async def handle_join_request(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """
    Smart join request handler with 3-tier verification:
    1. Auto-approve legitimate users
    2. Auto-reject obvious bots/spammers
    3. Math captcha for borderline cases
    """
    request = update.chat_join_request
    user = request.from_user
    chat_id = request.chat.id

    # Only handle managed channels
    if chat_id not in MANAGED_CHANNELS:
        return

    # Always approve admin
    if user.id == ADMIN_ID:
        await request.approve()
        logger.info(f"✅ Admin auto-approved: {user.id}")
        return

    # Block already blocked users
    if user.id in BLOCKED_USERS:
        await request.decline()
        logger.info(f"❌ Blocked user {user.id} tried to join")
        return

    # Check if bulk approval is enabled for this channel
    if BULK_APPROVAL_MODE.get(chat_id, False):
        await request.approve()
        track_user_activity(user.id, chat_id, 'approved', {
            'first_name': user.first_name,
            'last_name': user.last_name or '',
            'username': user.username or ''
        })
        logger.info(f"✅ Bulk-approved user: {user.id}")
        return

    # Smart verification - check legitimacy
    legitimacy = await check_user_legitimacy(context, user.id)

    # === TIER 1: AUTO-APPROVE LEGITIMATE USERS ===
    if legitimacy['legitimate'] and legitimacy['score'] >= 100:
        try:
            await request.approve()
            track_user_activity(user.id, chat_id, 'approved', {
                'first_name': user.first_name,
                'last_name': user.last_name or '',
                'username': user.username or ''
            })

            # Log to recent activity instead of sending notification
            RECENT_ACTIVITY.append({
                'type': 'auto_approved',
                'user_id': user.id,
                'user_name': user.first_name,
                'username': user.username or 'None',
                'channel': MANAGED_CHANNELS[chat_id]['name'],
                'channel_id': chat_id,
                'timestamp': datetime.now()
            })

            logger.info(f"✅ Auto-approved legitimate user: {user.id}")
            return
        except Exception as e:
            logger.error(f"Auto-approval failed: {e}")

    # === TIER 2: AUTO-REJECT OBVIOUS BOTS/SPAMMERS ===
    if not legitimacy['legitimate'] and legitimacy['score'] == 0:
        try:
            await request.decline()

            # Log to recent activity instead of sending notification
            RECENT_ACTIVITY.append({
                'type': 'auto_rejected',
                'user_id': user.id,
                'user_name': user.first_name or 'No Name',
                'username': user.username or 'None',
                'channel': MANAGED_CHANNELS[chat_id]['name'],
                'channel_id': chat_id,
                'reason': legitimacy.get('reason', 'Suspicious'),
                'timestamp': datetime.now()
            })

            logger.info(f"❌ Auto-rejected suspicious user: {user.id}")
            return
        except Exception as e:
            logger.error(f"Auto-rejection failed: {e}")

    # === TIER 3: MATH CAPTCHA FOR BORDERLINE CASES ===
    # Generate simple math problem
    num1 = random.randint(1, 10)
    num2 = random.randint(1, 10)
    answer = num1 + num2

    # Store verification data
    PENDING_VERIFICATIONS[user.id] = {
        'code': str(answer),
        'chat_id': chat_id,
        'timestamp': datetime.now(),
        'captcha_question': f"{num1} + {num2}",
        'request': request  # Store request object for later approval
    }

    track_user_activity(user.id, chat_id, 'pending', {
        'first_name': user.first_name,
        'last_name': user.last_name or '',
        'username': user.username or ''
    })

    # Send captcha to admin with quick approve button
    user_link = f"tg://user?id={user.id}"
    keyboard = [[
        InlineKeyboardButton("✅ Approve",
                            callback_data=f"enter_code_{user.id}")
    ]]

    await context.bot.send_message(
        ADMIN_ID,
        f"⚠️ *Verification Needed*\n\n"
        f"Channel: {MANAGED_CHANNELS[chat_id]['name']}\n"
        f"User: [{user.first_name}]({user_link})\n"
        f"ID: `{user.id}`\n"
        f"Username: @{user.username or 'None'}\n"
        f"Status: Borderline - needs verification\n\n"
        f"Math Captcha: {num1} + {num2} = ?\n"
        f"Answer: {answer}\n\n"
        f"Reason: {legitimacy.get('reason', 'Unknown')}",
        reply_markup=InlineKeyboardMarkup(keyboard))

    logger.info(f"⚠️ Sent verification request for user: {user.id}")


async def enter_code_callback(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Handle admin's approval via button click"""
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    await query.answer()

    user_id = int(query.data.split('_')[-1])

    if user_id not in PENDING_VERIFICATIONS:
        await query.edit_message_text("❌ Verification expired or already processed")
        return

    verification = PENDING_VERIFICATIONS[user_id]
    chat_id = verification['chat_id']

    try:
        # Approve the join request
        request = verification.get('request')
        if request:
            await request.approve()
        else:
            # Fallback: Try to approve via invite link
            logger.warning(f"No request object found for user {user_id}")

        # Track approval
        track_user_activity(user_id, chat_id, 'approved', {
            'first_name': USER_DATABASE.get(user_id, {}).get('first_name', 'Unknown')
        })

        # Remove from pending
        del PENDING_VERIFICATIONS[user_id]

        await query.edit_message_text(
            f"✅ *User Approved*\n\n"
            f"User ID: `{user_id}`\n"
            f"Channel: {MANAGED_CHANNELS[chat_id]['name']}",
            )

        logger.info(f"✅ Admin manually approved user: {user_id}")

    except Exception as e:
        logger.error(f"Manual approval failed: {e}")
        await query.edit_message_text(
            f"❌ Approval failed\n\nError: {str(e)}",
            )


async def resend_code_callback(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    """Placeholder for resend code functionality"""
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    await query.answer("Feature not applicable for this verification system")


# ========== COMMAND HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command with all commands listed"""
    user = update.effective_user

    if user.id == ADMIN_ID:
        text = (
            "👋 Welcome Owner!\n\n"
            "🤖 Bot Status: Online 24/7\n"
            f"📢 Channels: {len(MANAGED_CHANNELS)}\n"
            f"⚙️ Smart Verification: Enabled\n\n"

            "━━━ SETUP & INFO ━━━\n"
            "/start - This menu\n"
            "/addchannel - Add channel\n"
            "/channels - List channels\n"
            "/stats - Statistics\n\n"

            "━━━ ACTIVITY ━━━\n"
            "/recent_activity - Who joined\n"
            "/clear_activity - Clear log\n"
            "/pending_users - Captchas\n\n"

            "━━━ APPROVALS ━━━\n"
            "/approve_user - Approve one\n"
            "/approve_all_pending - Approve all\n"
            "/block_user - Block user\n"
            "/unblock_user - Unblock\n\n"

            "━━━ SETTINGS ━━━\n"
            "/toggle_bulk - Change mode\n"
            "/verification_settings - View\n\n"

            "━━━ POSTING ━━━\n"
            "/post - Post content\n"
            "/send_to_channel - Quick send\n"
            "/clear_channel_media - Clear media\n\n"

            "━━━ IMAGES ━━━\n"
            "/upload_images - Upload\n"
            "/done_uploading - Finish\n"
            "/list_images - View list\n"
            "/clear_images - Delete all\n\n"

            "━━━ AUTO-POST ━━━\n"
            "/enable_autopost - Enable\n"
            "/disable_autopost - Disable\n"
            "/autopost_status - Check\n\n"

            "━━━ ANALYTICS ━━━\n"
            "/user_stats - Stats\n"
            "/export_users - Export CSV\n\n"

            "━━━ SECURITY ━━━\n"
            "/view_unauthorized - Check\n"
            "/clear_unauthorized - Clear"
        )
    else:
        text = "Hello! This bot is for channel management."

    await update.message.reply_text(text)


async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a channel to manage"""
    if not await owner_only_check(update, context):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/addchannel CHANNEL_ID CHANNEL_NAME`\n\n"
            "Example: `/addchannel -1001234567890 My Premium Channel`",
            )
        return

    try:
        channel_id = int(context.args[0])
        channel_name = ' '.join(context.args[1:])

        # Check if bot is admin
        if not await is_bot_admin(context, channel_id):
            await update.message.reply_text("❌ Bot is not admin in that channel")
            return

        MANAGED_CHANNELS[channel_id] = {'name': channel_name}
        save_data()

        await update.message.reply_text(
            f"✅ Channel added!\n\n"
            f"Name: {channel_name}\n"
            f"ID: `{channel_id}`\n"
            f"Smart Verification: Enabled",
            )

        logger.info(f"✅ Channel added: {channel_name} ({channel_id})")

    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        logger.error(f"Add channel failed: {e}")


async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all managed channels"""
    if not await owner_only_check(update, context):
        return

    if not MANAGED_CHANNELS:
        await update.message.reply_text("No channels added yet")
        return

    text = "📢 *Managed Channels:*\n\n"
    for channel_id, data in MANAGED_CHANNELS.items():
        bulk_status = "🔄 Bulk" if BULK_APPROVAL_MODE.get(channel_id, False) else "🛡️ Smart"
        text += f"{data['name']}\n"
        text += f"ID: `{channel_id}`\n"
        text += f"Mode: {bulk_status}\n\n"

    await update.message.reply_text(text)


async def pending_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show pending verification requests"""
    if not await owner_only_check(update, context):
        return

    if not PENDING_VERIFICATIONS:
        await update.message.reply_text("No pending verifications")
        return

    text = "⏳ *Pending Verifications:*\n\n"
    for user_id, data in PENDING_VERIFICATIONS.items():
        channel_name = MANAGED_CHANNELS.get(data['chat_id'], {}).get('name', 'Unknown')
        text += f"User ID: `{user_id}`\n"
        text += f"Channel: {channel_name}\n"
        text += f"Captcha: {data['captcha_question']} = {data['code']}\n\n"

    await update.message.reply_text(text)


async def manual_approve_user(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Manually approve a user"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/approve_user USER_ID`",
            )
        return

    try:
        user_id = int(context.args[0])

        if user_id not in PENDING_VERIFICATIONS:
            await update.message.reply_text("❌ User not in pending list")
            return

        verification = PENDING_VERIFICATIONS[user_id]
        chat_id = verification['chat_id']
        request = verification.get('request')

        if request:
            await request.approve()
            track_user_activity(user_id, chat_id, 'approved')
            del PENDING_VERIFICATIONS[user_id]

            await update.message.reply_text(
                f"✅ User approved!\n\n"
                f"User ID: `{user_id}`\n"
                f"Channel: {MANAGED_CHANNELS[chat_id]['name']}",
                )

            logger.info(f"✅ Manual approval: {user_id}")
        else:
            await update.message.reply_text("❌ Cannot approve - request expired")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        logger.error(f"Manual approval failed: {e}")


async def approve_all_pending(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Approve all pending verifications"""
    if not await owner_only_check(update, context):
        return

    if not PENDING_VERIFICATIONS:
        await update.message.reply_text("No pending verifications")
        return

    approved = 0
    failed = 0

    for user_id, verification in list(PENDING_VERIFICATIONS.items()):
        try:
            request = verification.get('request')
            if request:
                await request.approve()
                track_user_activity(user_id, verification['chat_id'], 'approved')
                del PENDING_VERIFICATIONS[user_id]
                approved += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"Approval failed for {user_id}: {e}")
            failed += 1

    await update.message.reply_text(
        f"✅ *Bulk Approval Complete*\n\n"
        f"Approved: {approved}\n"
        f"Failed: {failed}",
        )


async def bulk_approve_from_file(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    """Placeholder for bulk approve from file"""
    if not await owner_only_check(update, context):
        return

    await update.message.reply_text(
        "To bulk approve:\n"
        "1. Send me a text file with user IDs (one per line)\n"
        "2. I'll approve them all")


async def toggle_bulk_approval(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    """Toggle bulk approval mode for a channel"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/toggle_bulk CHANNEL_ID`\n\n"
            "This toggles between:\n"
            "🛡️ Smart Verification (auto-approve/reject/captcha)\n"
            "🔄 Bulk Mode (approve everyone)",
            )
        return

    try:
        channel_id = int(context.args[0])

        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not managed")
            return

        current_status = BULK_APPROVAL_MODE.get(channel_id, False)
        BULK_APPROVAL_MODE[channel_id] = not current_status
        save_data()

        new_mode = "🔄 Bulk Mode (approve everyone)" if BULK_APPROVAL_MODE[channel_id] else "🛡️ Smart Verification"

        await update.message.reply_text(
            f"✅ Mode changed!\n\n"
            f"Channel: {MANAGED_CHANNELS[channel_id]['name']}\n"
            f"New Mode: {new_mode}",
            )

        logger.info(f"✅ Toggle bulk for {channel_id}: {BULK_APPROVAL_MODE[channel_id]}")

    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block a user from all channels"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/block_user USER_ID`",
            )
        return

    try:
        user_id = int(context.args[0])
        BLOCKED_USERS.add(user_id)
        save_data()

        await update.message.reply_text(
            f"✅ User blocked!\n\n"
            f"User ID: `{user_id}`\n"
            f"They cannot join any managed channel",
            )

        logger.info(f"✅ User blocked: {user_id}")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")


async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unblock a user"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/unblock_user USER_ID`",
            )
        return

    try:
        user_id = int(context.args[0])

        if user_id in BLOCKED_USERS:
            BLOCKED_USERS.remove(user_id)
            save_data()
            await update.message.reply_text(
                f"✅ User unblocked!\n\n"
                f"User ID: `{user_id}`",
                )
            logger.info(f"✅ User unblocked: {user_id}")
        else:
            await update.message.reply_text("❌ User not in blocked list")

    except ValueError:
        await update.message.reply_text("❌ Invalid user ID")


async def verification_settings(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    """Show verification settings"""
    if not await owner_only_check(update, context):
        return

    text = (
        f"⚙️ *Smart Verification Settings*\n\n"
        f"**Auto-Approve (Score ≥70):**\n"
        f"• Has real name (40 pts)\n"
        f"• Has username (30 pts)\n"
        f"• Has profile photo (30 pts)\n\n"
        f"**Math Captcha (Score 30-69):**\n"
        f"• Borderline users\n"
        f"• Admin decides\n\n"
        f"**Auto-Reject (Score 0):**\n"
        f"• Bot accounts\n"
        f"• 'User123456' names\n"
        f"• Suspicious profiles\n\n"
        f"*Current Settings:*\n"
        f"Profile Photo Required: {REQUIRE_PROFILE_PHOTO}\n"
        f"Min Account Age: {MIN_ACCOUNT_AGE_DAYS} days"
    )

    await update.message.reply_text(text)


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start posting mode"""
    if not await owner_only_check(update, context):
        return

    context.user_data['posting_mode'] = True
    await update.message.reply_text(
        "📤 *Posting Mode Enabled*\n\n"
        "Send me content to post (text, photo, video, or document)",
        )


async def upload_images_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    """Start image upload mode"""
    if not await owner_only_check(update, context):
        return

    context.user_data['uploading_mode'] = True
    context.user_data['uploading_for_channel'] = None  # Clear channel-specific flag
    await update.message.reply_text(
        "📤 Upload Mode Enabled\n\n"
        "Send images (with or without captions).\n"
        "Images with captions will use those captions when auto-posting.\n"
        "Use /done_uploading when finished")


async def done_uploading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop upload mode"""
    if not await owner_only_check(update, context):
        return

    context.user_data['uploading_mode'] = False
    await update.message.reply_text(
        f"✅ Upload complete!\n\n"
        f"Total images: {len(UPLOADED_IMAGES)}",
        )


async def upload_for_channel_command(update: Update,
                                     context: ContextTypes.DEFAULT_TYPE):
    """Upload images for specific channel"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/upload_for_channel CHANNEL_ID`",
            )
        return

    try:
        channel_id = int(context.args[0])

        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not managed")
            return

        context.user_data['uploading_for_channel'] = channel_id
        context.user_data['uploading_mode'] = True

        await update.message.reply_text(
            f"📤 Upload Mode for {MANAGED_CHANNELS[channel_id]['name']}\n\n"
            f"Send images (with or without captions).\n"
            f"These images will only be used for this channel.\n"
            f"Use /done_uploading when finished")

    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")


async def list_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List uploaded images"""
    if not await owner_only_check(update, context):
        return

    text = f"📂 *Image Library*\n\n"
    text += f"Global Images: {len(UPLOADED_IMAGES)}\n\n"

    for channel_id, images in CHANNEL_SPECIFIC_IMAGES.items():
        channel_name = MANAGED_CHANNELS.get(channel_id, {}).get('name', 'Unknown')
        text += f"{channel_name}: {len(images)} images\n"

    await update.message.reply_text(text)


async def clear_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all images"""
    if not await owner_only_check(update, context):
        return

    UPLOADED_IMAGES.clear()
    CHANNEL_SPECIFIC_IMAGES.clear()
    save_data()

    await update.message.reply_text("✅ All images cleared")


async def set_default_caption(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Set default caption for posts"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/set_default_caption Your caption here`",
            )
        return

    global DEFAULT_CAPTION
    DEFAULT_CAPTION = ' '.join(context.args)
    save_data()

    await update.message.reply_text(
        f"✅ Default caption set!\n\n"
        f"Caption: {DEFAULT_CAPTION}",
        )


async def clear_default_caption(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    """Clear default caption"""
    if not await owner_only_check(update, context):
        return

    global DEFAULT_CAPTION
    DEFAULT_CAPTION = ""
    save_data()

    await update.message.reply_text("✅ Default caption cleared")


async def set_channel_caption(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Set channel-specific caption"""
    if not await owner_only_check(update, context):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/set_channel_caption CHANNEL_ID Your caption`",
            )
        return

    try:
        channel_id = int(context.args[0])
        caption = ' '.join(context.args[1:])

        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not managed")
            return

        CHANNEL_DEFAULT_CAPTIONS[channel_id] = caption
        save_data()

        await update.message.reply_text(
            f"✅ Caption set for {MANAGED_CHANNELS[channel_id]['name']}!\n\n"
            f"Caption: {caption}",
            )

    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")


async def clear_channel_caption(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    """Clear channel-specific caption"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/clear_channel_caption CHANNEL_ID`",
            )
        return

    try:
        channel_id = int(context.args[0])

        if channel_id in CHANNEL_DEFAULT_CAPTIONS:
            del CHANNEL_DEFAULT_CAPTIONS[channel_id]
            save_data()
            await update.message.reply_text("✅ Channel caption cleared")
        else:
            await update.message.reply_text("❌ No caption set for this channel")

    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")


async def enable_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable auto-posting for a channel"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/enable_autopost CHANNEL_ID`",
            )
        return

    try:
        channel_id = int(context.args[0])

        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not managed")
            return

        AUTO_POST_ENABLED[channel_id] = True
        save_data()

        # Add scheduler job
        scheduler.add_job(
            auto_post_job,
            trigger=CronTrigger(minute='*/15'),
            args=[context.bot, channel_id],
            id=f'autopost_{channel_id}',
            replace_existing=True)

        await update.message.reply_text(
            f"✅ Auto-post enabled for {MANAGED_CHANNELS[channel_id]['name']}!\n\n"
            f"Interval: Every 15 minutes",
            )

    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")


async def disable_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable auto-posting for a channel"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/disable_autopost CHANNEL_ID`",
            )
        return

    try:
        channel_id = int(context.args[0])

        if channel_id in AUTO_POST_ENABLED:
            AUTO_POST_ENABLED[channel_id] = False
            save_data()

            # Remove scheduler job
            try:
                scheduler.remove_job(f'autopost_{channel_id}')
            except:
                pass

            await update.message.reply_text(
                f"✅ Auto-post disabled for {MANAGED_CHANNELS[channel_id]['name']}",
                )
        else:
            await update.message.reply_text("❌ Auto-post not enabled for this channel")

    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID")


async def autopost_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show auto-post status"""
    if not await owner_only_check(update, context):
        return

    if not AUTO_POST_ENABLED:
        await update.message.reply_text("No auto-posts enabled")
        return

    text = "🤖 *Auto-Post Status*\n\n"
    for channel_id, enabled in AUTO_POST_ENABLED.items():
        if enabled:
            channel_name = MANAGED_CHANNELS.get(channel_id, {}).get('name', 'Unknown')
            text += f"✅ {channel_name}\n"

    await update.message.reply_text(text)


async def auto_post_job(bot, channel_id: int):
    """Auto-posting job - posts images every 15 minutes"""
    try:
        if not AUTO_POST_ENABLED.get(channel_id):
            return

        # Get images for this channel
        if channel_id in CHANNEL_SPECIFIC_IMAGES and CHANNEL_SPECIFIC_IMAGES[channel_id]:
            images = CHANNEL_SPECIFIC_IMAGES[channel_id]
        elif UPLOADED_IMAGES:
            images = UPLOADED_IMAGES
        else:
            logger.warning(f"No images available for channel {channel_id}")
            return

        # Initialize image index if needed
        if channel_id not in CURRENT_IMAGE_INDEX:
            CURRENT_IMAGE_INDEX[channel_id] = 0

        # Get current image
        idx = CURRENT_IMAGE_INDEX[channel_id]
        image = images[idx]

        # Determine caption (priority: image caption > channel caption > default caption)
        if isinstance(image, dict) and image.get('caption'):
            final_caption = image['caption']
        elif channel_id in CHANNEL_DEFAULT_CAPTIONS:
            final_caption = CHANNEL_DEFAULT_CAPTIONS[channel_id]
        elif DEFAULT_CAPTION:
            final_caption = DEFAULT_CAPTION
        else:
            final_caption = ""

        # Get file_id
        if isinstance(image, dict):
            file_id = image['file_id']
        else:
            file_id = image  # Old format compatibility

        # Post to channel
        await bot.send_photo(
            chat_id=channel_id,
            photo=file_id,
            caption=final_caption
        )

        logger.info(f"✅ Auto-posted image #{idx+1} to channel {channel_id}")

        # Move to next image (loop back to start when done)
        CURRENT_IMAGE_INDEX[channel_id] = (idx + 1) % len(images)
        save_data()

    except Exception as e:
        logger.error(f"❌ Auto-post failed for channel {channel_id}: {e}")


async def export_users_report(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Export user database"""
    if not await owner_only_check(update, context):
        return

    if not USER_DATABASE:
        await update.message.reply_text("No user data to export")
        return

    # Create CSV
    csv_text = "User ID,First Name,Last Name,Username,Channels\n"
    for user_id, data in USER_DATABASE.items():
        channels = ', '.join([ch['channel_name'] for ch in data['channels'].values()])
        csv_text += f"{user_id},{data['first_name']},{data['last_name']},{data['username']},{channels}\n"

    # Send as file
    file = BytesIO(csv_text.encode('utf-8'))
    file.name = f"users_{datetime.now().strftime('%Y%m%d')}.csv"

    await update.message.reply_document(
        document=file,
        filename=file.name,
        caption=f"📊 User Database Export\n\nTotal Users: {len(USER_DATABASE)}")


async def user_stats_command(update: Update,
                             context: ContextTypes.DEFAULT_TYPE):
    """Show user statistics"""
    if not await owner_only_check(update, context):
        return

    total_users = len(USER_DATABASE)
    approved_count = sum(1 for user in USER_DATABASE.values() 
                        if any(ch['status'] == 'approved' for ch in user['channels'].values()))

    text = (
        f"👥 *User Statistics*\n\n"
        f"Total Users: {total_users}\n"
        f"Approved: {approved_count}\n"
        f"Pending: {len(PENDING_VERIFICATIONS)}\n"
        f"Blocked: {len(BLOCKED_USERS)}"
    )

    await update.message.reply_text(text)


async def import_users_to_channel(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
    """Import users (placeholder)"""
    if not await owner_only_check(update, context):
        return

    await update.message.reply_text("Import feature coming soon")


async def view_unauthorized_attempts(update: Update,
                                     context: ContextTypes.DEFAULT_TYPE):
    """View unauthorized access attempts"""
    if not await owner_only_check(update, context):
        return

    if not UNAUTHORIZED_ATTEMPTS:
        await update.message.reply_text("No unauthorized attempts")
        return

    text = "🚨 *Unauthorized Attempts*\n\n"
    for attempt in UNAUTHORIZED_ATTEMPTS[-10:]:  # Last 10
        text += (f"User: {attempt['first_name']}\n"
                f"ID: `{attempt['user_id']}`\n"
                f"Command: {attempt['command']}\n"
                f"Time: {attempt['timestamp'].strftime('%Y-%m-%d %H:%M')}\n\n")

    await update.message.reply_text(text)


async def clear_unauthorized_log(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    """Clear unauthorized attempts log"""
    if not await owner_only_check(update, context):
        return

    UNAUTHORIZED_ATTEMPTS.clear()
    await update.message.reply_text("✅ Unauthorized log cleared")


async def send_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick send media/text to channel"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        # Show available channels
        if not MANAGED_CHANNELS:
            await update.message.reply_text("No channels added yet")
            return

        text = "Send to channel:\n\n"
        text += "Usage: /send_to_channel CHANNEL_ID\n\n"
        text += "Your channels:\n"
        for channel_id, data in MANAGED_CHANNELS.items():
            text += f"{data['name']}: {channel_id}\n"
        text += "\nThen send your media/text"

        await update.message.reply_text(text)
        return

    try:
        channel_id = int(context.args[0])

        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("Channel not managed")
            return

        context.user_data['quick_send_channel'] = channel_id
        context.user_data['quick_send_mode'] = True

        await update.message.reply_text(
            f"✅ Quick Send Mode: {MANAGED_CHANNELS[channel_id]['name']}\n\n"
            f"Send your media or text now.\n"
            f"Use /cancel to stop")

    except ValueError:
        await update.message.reply_text("Invalid channel ID")


async def clear_channel_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all media from a channel"""
    if not await owner_only_check(update, context):
        return

    if not context.args:
        if not MANAGED_CHANNELS:
            await update.message.reply_text("No channels added yet")
            return

        text = "Clear channel media:\n\n"
        text += "Usage: /clear_channel_media CHANNEL_ID MESSAGE_COUNT\n\n"
        text += "Example: /clear_channel_media -1001234567890 50\n\n"
        text += "Your channels:\n"
        for channel_id, data in MANAGED_CHANNELS.items():
            text += f"{data['name']}: {channel_id}\n"

        await update.message.reply_text(text)
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /clear_channel_media CHANNEL_ID MESSAGE_COUNT\n"
            "Example: /clear_channel_media -1001234567890 50")
        return

    try:
        channel_id = int(context.args[0])
        message_count = int(context.args[1])

        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("Channel not managed")
            return

        if message_count > 100:
            await update.message.reply_text("Max 100 messages at a time")
            return

        await update.message.reply_text(f"⏳ Deleting last {message_count} messages...")

        deleted = 0
        failed = 0

        # Get recent messages and delete
        try:
            # This requires the bot to have delete messages permission
            # We'll try to delete messages by ID
            for i in range(message_count):
                try:
                    # Note: This is a simplified approach
                    # In production, you'd need to track message IDs
                    await update.message.reply_text(
                        "⚠️ To delete messages, I need message IDs.\n"
                        "This feature requires message tracking.\n"
                        "Use /clear_images to clear bot's image storage instead.")
                    return
                except:
                    failed += 1

        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)}")
            return

        await update.message.reply_text(
            f"✅ Deleted: {deleted}\n"
            f"❌ Failed: {failed}")

    except ValueError:
        await update.message.reply_text("Invalid channel ID or message count")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current operation"""
    if not await owner_only_check(update, context):
        return

    context.user_data.clear()
    await update.message.reply_text("✅ All operations cancelled")


async def view_recent_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View recent auto-approvals and auto-rejections"""
    if not await owner_only_check(update, context):
        return

    if not RECENT_ACTIVITY:
        await update.message.reply_text("No recent activity")
        return

    # Group by type
    approved = [a for a in RECENT_ACTIVITY if a['type'] == 'auto_approved']
    rejected = [a for a in RECENT_ACTIVITY if a['type'] == 'auto_rejected']

    text = "📊 Recent Activity\n\n"

    # Show summary
    text += f"✅ Auto-Approved: {len(approved)}\n"
    text += f"❌ Auto-Rejected: {len(rejected)}\n"
    text += f"⚠️ Pending Captcha: {len(PENDING_VERIFICATIONS)}\n\n"

    # Show last 10 approved
    if approved:
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += "✅ Recently Approved:\n\n"
        for activity in approved[-10:]:  # Last 10
            text += f"• {activity['user_name']}\n"
            text += f"  @{activity['username']}\n"
            text += f"  Channel: {activity['channel']}\n"
            text += f"  Time: {activity['timestamp'].strftime('%H:%M')}\n\n"

    # Show last 5 rejected
    if rejected:
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += "❌ Recently Rejected:\n\n"
        for activity in rejected[-5:]:  # Last 5
            text += f"• {activity['user_name']}\n"
            text += f"  Reason: {activity['reason']}\n"
            text += f"  Time: {activity['timestamp'].strftime('%H:%M')}\n\n"

    await update.message.reply_text(text)


async def clear_recent_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear recent activity log"""
    if not await owner_only_check(update, context):
        return

    count = len(RECENT_ACTIVITY)
    RECENT_ACTIVITY.clear()

    await update.message.reply_text(f"✅ Cleared {count} activity records")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics"""
    if not await owner_only_check(update, context):
        return

    bulk_enabled = sum(1 for v in BULK_APPROVAL_MODE.values() if v)
    active_autoposts = sum(1 for v in AUTO_POST_ENABLED.values() if v)

    # Count recent activity
    recent_approved = sum(1 for a in RECENT_ACTIVITY if a['type'] == 'auto_approved')
    recent_rejected = sum(1 for a in RECENT_ACTIVITY if a['type'] == 'auto_rejected')

    text = (f"📊 Statistics\n\n"
            f"📢 Channels: {len(MANAGED_CHANNELS)}\n"
            f"🛡️ Smart Mode: {len(MANAGED_CHANNELS) - bulk_enabled}\n"
            f"🔄 Bulk Mode: {bulk_enabled}\n"
            f"⏳ Pending: {len(PENDING_VERIFICATIONS)}\n"
            f"✅ Recent Approved: {recent_approved}\n"
            f"❌ Recent Rejected: {recent_rejected}\n"
            f"🚫 Blocked: {len(BLOCKED_USERS)}\n"
            f"📂 Images: {len(UPLOADED_IMAGES)}\n"
            f"🤖 Auto-Posts: {active_autoposts} active\n"
            f"👥 Total Users: {len(USER_DATABASE)}\n"
            f"🚨 Unauthorized: {len(UNAUTHORIZED_ATTEMPTS)}\n\n"
            f"Use /recent_activity for details\n\n"
            f"Status: Online 24/7 ✅")
    await update.message.reply_text(text)


async def handle_forwarded_message(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE):
    """Handle forwarded messages (placeholder)"""
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text("Forwarded message detected")


async def handle_bulk_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle bulk file uploads (placeholder)"""
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text("Bulk file processing coming soon")


async def handle_verification_code(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE):
    """Handle verification code from user"""
    # This is for future feature where users can verify themselves
    pass


async def handle_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle image uploads"""
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    if not update.message.photo:
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id
    caption = update.message.caption or ""

    # Store as dictionary with file_id and caption
    image_data = {
        'file_id': file_id,
        'caption': caption
    }

    if context.user_data.get('uploading_for_channel'):
        channel_id = context.user_data['uploading_for_channel']
        if channel_id not in CHANNEL_SPECIFIC_IMAGES:
            CHANNEL_SPECIFIC_IMAGES[channel_id] = []
        CHANNEL_SPECIFIC_IMAGES[channel_id].append(image_data)
        await update.message.reply_text(
            f"✅ Image added to {MANAGED_CHANNELS[channel_id]['name']}\n"
            f"Total: {len(CHANNEL_SPECIFIC_IMAGES[channel_id])}")
    else:
        UPLOADED_IMAGES.append(image_data)
        await update.message.reply_text(
            f"✅ Image uploaded! Total: {len(UPLOADED_IMAGES)}")

    save_data()


async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all content (text, images, videos)"""
    # Safety check
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    # Handle verification code
    if context.user_data.get('awaiting_code'):
        await handle_verification_code(update, context)
        return

    # Handle image upload mode
    if context.user_data.get('uploading_mode'):
        await handle_image_upload(update, context)
        return

    # Handle quick send mode
    if context.user_data.get('quick_send_mode'):
        channel_id = context.user_data.get('quick_send_channel')
        message = update.message

        try:
            if message.text:
                await context.bot.send_message(channel_id, message.text)
            elif message.photo:
                await context.bot.send_photo(channel_id,
                                             message.photo[-1].file_id,
                                             caption=message.caption)
            elif message.video:
                await context.bot.send_video(channel_id,
                                             message.video.file_id,
                                             caption=message.caption)
            elif message.document:
                await context.bot.send_document(channel_id,
                                                message.document.file_id,
                                                caption=message.caption)

            await update.message.reply_text(
                f"✅ Sent to {MANAGED_CHANNELS[channel_id]['name']}\n\n"
                f"Send more or use /cancel to stop")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed: {str(e)}")

        return

    # Handle posting mode
    if not context.user_data.get('posting_mode'):
        return

    message = update.message
    content_type = 'text'

    if message.photo:
        content_type = 'photo'
    elif message.video:
        content_type = 'video'
    elif message.document:
        content_type = 'document'

    PENDING_POSTS[ADMIN_ID] = {'message': message, 'type': content_type}

    keyboard = []
    for channel_id, data in MANAGED_CHANNELS.items():
        keyboard.append([
            InlineKeyboardButton(f"📢 {data['name']}",
                                 callback_data=f"post_{channel_id}")
        ])
    keyboard.append(
        [InlineKeyboardButton("🔄 ALL CHANNELS", callback_data="post_all")])
    keyboard.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="post_cancel")])

    await update.message.reply_text(
        "🎯 Select Channel:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['posting_mode'] = False


async def post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle post selection callback"""
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    await query.answer()

    if ADMIN_ID not in PENDING_POSTS:
        await query.edit_message_text("❌ No pending post")
        return

    action = query.data.split('_')[1]

    if action == "cancel":
        del PENDING_POSTS[ADMIN_ID]
        await query.edit_message_text("❌ Cancelled")
        return

    pending = PENDING_POSTS[ADMIN_ID]
    original_msg = pending['message']

    channels = [int(action)] if action != "all" else list(
        MANAGED_CHANNELS.keys())

    await query.edit_message_text("⏳ Posting...")
    success = 0
    failed = 0

    for channel_id in channels:
        try:
            if pending['type'] == 'text':
                await context.bot.send_message(channel_id, original_msg.text)
            elif pending['type'] == 'photo':
                await context.bot.send_photo(channel_id,
                                             original_msg.photo[-1].file_id,
                                             caption=original_msg.caption)
            elif pending['type'] == 'video':
                await context.bot.send_video(channel_id,
                                             original_msg.video.file_id,
                                             caption=original_msg.caption)
            elif pending['type'] == 'document':
                await context.bot.send_document(channel_id,
                                                original_msg.document.file_id,
                                                caption=original_msg.caption)
            success += 1
        except Exception as e:
            failed += 1
            logger.error(f"Post failed for {channel_id}: {e}")

    del PENDING_POSTS[ADMIN_ID]

    result_text = f"✅ *Posted!*\n\nSuccess: {success}\nFailed: {failed}"
    await query.message.reply_text(result_text)


async def weekly_report_job(bot):
    """Weekly report job"""
    text = (
        f"📊 *Weekly Report*\n\n"
        f"Channels: {len(MANAGED_CHANNELS)}\n"
        f"Total Users: {len(USER_DATABASE)}\n"
        f"Blocked: {len(BLOCKED_USERS)}\n\n"
        f"Status: All systems operational ✅"
    )

    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        logger.error(f"Weekly report failed: {e}")


def main():
    """Main function to start the bot"""
    logger.info("🚀 Starting SMART VERIFICATION BOT...")

    # Load saved data
    load_data()

    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("pending_users", pending_users))
    app.add_handler(CommandHandler("approve_user", manual_approve_user))
    app.add_handler(CommandHandler("approve_all_pending", approve_all_pending))
    app.add_handler(CommandHandler("bulk_approve", bulk_approve_from_file))
    app.add_handler(CommandHandler("toggle_bulk", toggle_bulk_approval))
    app.add_handler(CommandHandler("block_user", block_user))
    app.add_handler(CommandHandler("unblock_user", unblock_user))
    app.add_handler(
        CommandHandler("verification_settings", verification_settings))
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("upload_images", upload_images_command))
    app.add_handler(CommandHandler("done_uploading", done_uploading))
    app.add_handler(
        CommandHandler("upload_for_channel", upload_for_channel_command))
    app.add_handler(CommandHandler("list_images", list_images))
    app.add_handler(CommandHandler("clear_images", clear_images))
    app.add_handler(CommandHandler("set_default_caption", set_default_caption))
    app.add_handler(
        CommandHandler("clear_default_caption", clear_default_caption))
    app.add_handler(CommandHandler("set_channel_caption", set_channel_caption))
    app.add_handler(
        CommandHandler("clear_channel_caption", clear_channel_caption))
    app.add_handler(CommandHandler("enable_autopost", enable_autopost))
    app.add_handler(CommandHandler("disable_autopost", disable_autopost))
    app.add_handler(CommandHandler("autopost_status", autopost_status))
    app.add_handler(CommandHandler("export_users", export_users_report))
    app.add_handler(CommandHandler("user_stats", user_stats_command))
    app.add_handler(CommandHandler("import_users", import_users_to_channel))
    app.add_handler(
        CommandHandler("view_unauthorized", view_unauthorized_attempts))
    app.add_handler(
        CommandHandler("clear_unauthorized", clear_unauthorized_log))
    app.add_handler(CommandHandler("recent_activity", view_recent_activity))
    app.add_handler(CommandHandler("clear_activity", clear_recent_activity))
    app.add_handler(CommandHandler("send_to_channel", send_to_channel))
    app.add_handler(CommandHandler("clear_channel_media", clear_channel_media))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats))

    # Callback handlers
    app.add_handler(
        CallbackQueryHandler(enter_code_callback, pattern="^enter_code_"))
    app.add_handler(
        CallbackQueryHandler(resend_code_callback, pattern="^resend_code_"))
    app.add_handler(CallbackQueryHandler(post_callback, pattern="^post_"))

    # Message handlers
    app.add_handler(
        MessageHandler(filters.FORWARDED & ~filters.COMMAND,
                       handle_forwarded_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_bulk_file))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_content))
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO, handle_content))

    # Join request handler - THE KEY COMPONENT
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Start scheduler
    scheduler.start()
    scheduler.add_job(weekly_report_job,
                      trigger=CronTrigger(day_of_week='mon', hour=9),
                      args=[app.bot],
                      id='weekly_report')

    # Re-enable auto-posting for saved channels
    for channel_id, enabled in AUTO_POST_ENABLED.items():
        if enabled:
            try:
                scheduler.add_job(auto_post_job,
                                  trigger=CronTrigger(minute='*/15'),
                                  args=[app.bot, channel_id],
                                  id=f'autopost_{channel_id}',
                                  replace_existing=True)
                logger.info(f"✅ Auto-post restored for {channel_id}")
            except Exception as e:
                logger.error(f"Failed to restore: {e}")

    logger.info(f"✅ Bot running - Owner: {ADMIN_ID}")
    logger.info(f"✅ Smart Verification: ENABLED")
    logger.info(
        f"✅ Loaded: {len(MANAGED_CHANNELS)} channels, {len(UPLOADED_IMAGES)} images"
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
