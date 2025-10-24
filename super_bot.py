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
    if not name or len(name) < 2:
        return True
    letters_and_numbers = re.sub(r'[^a-zA-Z0-9]', '', name)
    if len(letters_and_numbers) < 2:
        return True
    return False


async def check_user_legitimacy(context: ContextTypes.DEFAULT_TYPE,
                                user_id: int) -> dict:
    try:
        user = await context.bot.get_chat(user_id)
        if user.type == "bot":
            return {"legitimate": False, "reason": "Bot", "score": 0}
        if not user.first_name or is_name_suspicious(user.first_name):
            return {
                "legitimate": False,
                "reason": "Suspicious name",
                "score": 0
            }
        return {"legitimate": True, "score": 100}
    except:
        return {"legitimate": False, "reason": "Error", "score": 0}


def track_user_activity(user_id: int,
                        channel_id: int,
                        action: str,
                        user_data: dict = None):
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
            f"⚠️ *Unauthorized Access*\n\n"
            f"User: {first_name}\n"
            f"Username: @{username if username else 'None'}\n"
            f"ID: `{user_id}`\n"
            f"Command: /{command}",
            parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to alert owner: {e}")


async def owner_only_check(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Check if update has effective_user (channel posts don't have users)
    if not update.effective_user:
        logger.warning("Received update without effective_user")
        return False
    
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        username = update.effective_user.username or ""
        first_name = update.effective_user.first_name or "Unknown"
        command = update.message.text.split()[0].replace('/', '') if update.message and update.message.text else "unknown"
        await alert_owner_unauthorized_access(context, user_id, username,
                                             first_name, command)
        await update.message.reply_text("❌ Unauthorized")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *SUPER BOT Active*\n\nAdmin only.",
                                   parse_mode='Markdown')


async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    await update.message.reply_text("📢 Forward channel message to add.")


async def handle_forwarded_message(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    fwd = update.message.forward_from_chat
    if not fwd or fwd.type != 'channel':
        await update.message.reply_text("❌ Not a channel message")
        return

    channel_id = fwd.id
    if channel_id in MANAGED_CHANNELS:
        await update.message.reply_text("⚠️ Already added!")
        return

    if not await is_bot_admin(context, channel_id):
        await update.message.reply_text("❌ Bot not admin in channel")
        return

    channel_name = fwd.title
    MANAGED_CHANNELS[channel_id] = {'name': channel_name, 'mode': 'manual'}
    BULK_APPROVAL_MODE[channel_id] = False
    save_data()
    await update.message.reply_text(
        f"✅ *Added:* {channel_name}\nID: `{channel_id}`", parse_mode='Markdown')


async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not MANAGED_CHANNELS:
        await update.message.reply_text("📢 No channels")
        return

    text = "📢 *Managed Channels:*\n\n"
    for cid, data in MANAGED_CHANNELS.items():
        bulk_mode = BULK_APPROVAL_MODE.get(cid, False)
        auto_post = AUTO_POST_ENABLED.get(cid, False)
        text += f"• {data['name']}\n"
        text += f"  ID: `{cid}`\n"
        text += f"  Bulk: {'ON' if bulk_mode else 'OFF'}\n"
        text += f"  Auto-Post: {'ON' if auto_post else 'OFF'}\n\n"

    await update.message.reply_text(text, parse_mode='Markdown')


async def handle_join_request(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    user = request.from_user
    chat_id = request.chat.id

    if chat_id not in MANAGED_CHANNELS:
        return

    if user.id == ADMIN_ID:
        await request.approve()
        return

    if user.id in BLOCKED_USERS:
        await request.decline()
        return

    if BULK_APPROVAL_MODE.get(chat_id, False):
        await request.approve()
        track_user_activity(user.id, chat_id, 'approved', {
            'first_name': user.first_name,
            'last_name': user.last_name or '',
            'username': user.username or ''
        })
        return

    track_user_activity(user.id, chat_id, 'pending', {
        'first_name': user.first_name,
        'last_name': user.last_name or '',
        'username': user.username or ''
    })

    legitimacy = await check_user_legitimacy(context, user.id)
    code = generate_verification_code()

    PENDING_VERIFICATIONS[user.id] = {
        'code': code,
        'chat_id': chat_id,
        'timestamp': datetime.now()
    }

    user_link = f"tg://user?id={user.id}"
    keyboard = [[
        InlineKeyboardButton("✅ Auto-Approve",
                            callback_data=f"enter_code_{user.id}")
    ]]

    await context.bot.send_message(
        ADMIN_ID,
        f"🔔 *Join Request*\n\n"
        f"Channel: {MANAGED_CHANNELS[chat_id]['name']}\n"
        f"User: [{user.first_name}]({user_link})\n"
        f"ID: `{user.id}`\n"
        f"Username: @{user.username or 'None'}\n"
        f"Status: {'✅ Legit' if legitimacy['legitimate'] else '⚠️ Suspicious'}\n\n"
        f"Code: `{code}`",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard))


async def enter_code_callback(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    user_id = int(query.data.split('_')[-1])

    if user_id not in PENDING_VERIFICATIONS:
        await query.answer("❌ Expired", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text(query.message.text + "\n\n✅ *Auto-Approved*",
                                 parse_mode='Markdown')

    data = PENDING_VERIFICATIONS[user_id]
    chat_id = data['chat_id']

    try:
        await context.bot.approve_chat_join_request(chat_id, user_id)
        VERIFIED_FOR_CHANNELS.setdefault(user_id, set()).add(chat_id)
        track_user_activity(user_id, chat_id, 'approved')
        del PENDING_VERIFICATIONS[user_id]
    except Exception as e:
        await query.message.reply_text(f"❌ Approval failed: {e}")


async def resend_code_callback(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    user_id = int(query.data.split('_')[-1])

    if user_id not in PENDING_VERIFICATIONS:
        await query.answer("❌ Expired", show_alert=True)
        return

    data = PENDING_VERIFICATIONS[user_id]
    code = data['code']

    await query.answer(f"Code: {code}", show_alert=True)


async def handle_verification_code(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()

    for user_id, data in PENDING_VERIFICATIONS.items():
        if data['code'] == code:
            chat_id = data['chat_id']
            try:
                await context.bot.approve_chat_join_request(chat_id, user_id)
                await update.message.reply_text("✅ User approved!")
                VERIFIED_FOR_CHANNELS.setdefault(user_id, set()).add(chat_id)
                track_user_activity(user_id, chat_id, 'approved')
                del PENDING_VERIFICATIONS[user_id]
                context.user_data['awaiting_code'] = False
            except Exception as e:
                await update.message.reply_text(f"❌ Failed: {e}")
            return

    await update.message.reply_text("❌ Invalid code")


async def pending_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not PENDING_VERIFICATIONS:
        await update.message.reply_text("✅ No pending users")
        return

    text = "⏳ *Pending Users:*\n\n"
    for user_id, data in PENDING_VERIFICATIONS.items():
        chat_name = MANAGED_CHANNELS.get(data['chat_id'], {}).get('name', 'Unknown')
        text += f"• User ID: `{user_id}`\n"
        text += f"  Channel: {chat_name}\n"
        text += f"  Code: `{data['code']}`\n\n"

    await update.message.reply_text(text, parse_mode='Markdown')


async def manual_approve_user(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /approve_user <user_id> <channel_id>")
        return

    try:
        user_id = int(context.args[0])
        channel_id = int(context.args[1])

        await context.bot.approve_chat_join_request(channel_id, user_id)
        VERIFIED_FOR_CHANNELS.setdefault(user_id, set()).add(channel_id)
        track_user_activity(user_id, channel_id, 'approved')

        await update.message.reply_text("✅ User approved!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


async def approve_all_pending(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not PENDING_VERIFICATIONS:
        await update.message.reply_text("✅ No pending users")
        return

    approved = 0
    failed = 0

    for user_id, data in list(PENDING_VERIFICATIONS.items()):
        try:
            await context.bot.approve_chat_join_request(data['chat_id'],
                                                       user_id)
            VERIFIED_FOR_CHANNELS.setdefault(user_id, set()).add(
                data['chat_id'])
            track_user_activity(user_id, data['chat_id'], 'approved')
            del PENDING_VERIFICATIONS[user_id]
            approved += 1
        except Exception as e:
            failed += 1
            logger.error(f"Failed to approve {user_id}: {e}")

    await update.message.reply_text(
        f"✅ Approved: {approved}\n❌ Failed: {failed}")


async def toggle_bulk_approval(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /toggle_bulk <channel_id>")
        return

    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not managed")
            return

        BULK_APPROVAL_MODE[channel_id] = not BULK_APPROVAL_MODE.get(
            channel_id, False)
        status = "ON" if BULK_APPROVAL_MODE[channel_id] else "OFF"
        save_data()
        await update.message.reply_text(
            f"✅ Bulk approval for {MANAGED_CHANNELS[channel_id]['name']}: *{status}*",
            parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def bulk_approve_from_file(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    await update.message.reply_text("📂 Send text file with user IDs")
    context.user_data['awaiting_bulk_file'] = True


async def handle_bulk_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_bulk_file'):
        return

    if update.message.from_user.id != ADMIN_ID:
        return

    document = update.message.document
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Send .txt file")
        return

    file = await context.bot.get_file(document.file_id)
    file_bytes = await file.download_as_bytearray()
    content = file_bytes.decode('utf-8')

    user_ids = []
    for line in content.split('\n'):
        line = line.strip()
        if line.isdigit():
            user_ids.append(int(line))

    if not user_ids:
        await update.message.reply_text("❌ No user IDs found")
        return

    await update.message.reply_text(
        f"Found {len(user_ids)} users. Reply with channel ID:")
    context.user_data['bulk_users'] = user_ids
    context.user_data['awaiting_channel_id'] = True
    context.user_data['awaiting_bulk_file'] = False


async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /block_user <user_id>")
        return

    try:
        user_id = int(context.args[0])
        BLOCKED_USERS.add(user_id)
        save_data()
        await update.message.reply_text(f"✅ User `{user_id}` blocked",
                                       parse_mode='Markdown')
    except:
        await update.message.reply_text("❌ Invalid user ID")


async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /unblock_user <user_id>")
        return

    try:
        user_id = int(context.args[0])
        if user_id in BLOCKED_USERS:
            BLOCKED_USERS.remove(user_id)
            save_data()
            await update.message.reply_text(f"✅ User `{user_id}` unblocked",
                                           parse_mode='Markdown')
        else:
            await update.message.reply_text("⚠️ User not blocked")
    except:
        await update.message.reply_text("❌ Invalid user ID")


async def verification_settings(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    text = (f"🔒 *Verification Settings*\n\n"
            f"Min Account Age: {MIN_ACCOUNT_AGE_DAYS} days\n"
            f"Require Photo: {REQUIRE_PROFILE_PHOTO}\n"
            f"Code Expiry: {CODE_EXPIRY_MINUTES} minutes")

    await update.message.reply_text(text, parse_mode='Markdown')


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not MANAGED_CHANNELS:
        await update.message.reply_text("❌ No channels added")
        return

    await update.message.reply_text("📤 Send content to post:")
    context.user_data['posting_mode'] = True


async def upload_images_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    await update.message.reply_text(
        "📷 *Upload Mode Active*\n\nSend images. Use /done_uploading when finished.",
        parse_mode='Markdown')
    context.user_data['uploading_mode'] = True


async def handle_image_upload(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        UPLOADED_IMAGES.append(file_id)
        save_data()
        await update.message.reply_text(
            f"✅ Image {len(UPLOADED_IMAGES)} saved")


async def done_uploading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    context.user_data['uploading_mode'] = False
    await update.message.reply_text(
        f"✅ Upload complete! Total images: {len(UPLOADED_IMAGES)}")


async def upload_for_channel_command(update: Update,
                                     context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /upload_for_channel <channel_id>")
        return

    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not managed")
            return

        context.user_data['uploading_for_channel'] = channel_id
        context.user_data['uploading_mode'] = True
        await update.message.reply_text(
            f"📷 Upload images for {MANAGED_CHANNELS[channel_id]['name']}")
    except:
        await update.message.reply_text("❌ Invalid channel ID")


async def list_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    text = f"📂 *Images:* {len(UPLOADED_IMAGES)}\n\n"

    for channel_id, images in CHANNEL_SPECIFIC_IMAGES.items():
        channel_name = MANAGED_CHANNELS.get(channel_id, {}).get('name', 'Unknown')
        text += f"• {channel_name}: {len(images)} images\n"

    await update.message.reply_text(text, parse_mode='Markdown')


async def clear_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    UPLOADED_IMAGES.clear()
    CHANNEL_SPECIFIC_IMAGES.clear()
    CURRENT_IMAGE_INDEX.clear()
    save_data()
    await update.message.reply_text("✅ All images cleared")


async def set_default_caption(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /set_default_caption <text>")
        return

    global DEFAULT_CAPTION
    DEFAULT_CAPTION = ' '.join(context.args)
    save_data()
    await update.message.reply_text(f"✅ Default caption set:\n\n{DEFAULT_CAPTION}")


async def clear_default_caption(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    global DEFAULT_CAPTION
    DEFAULT_CAPTION = ""
    save_data()
    await update.message.reply_text("✅ Default caption cleared")


async def set_channel_caption(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /set_channel_caption <channel_id> <text>")
        return

    try:
        channel_id = int(context.args[0])
        caption = ' '.join(context.args[1:])
        CHANNEL_DEFAULT_CAPTIONS[channel_id] = caption
        save_data()
        await update.message.reply_text(
            f"✅ Caption set for {MANAGED_CHANNELS.get(channel_id, {}).get('name', 'channel')}"
        )
    except:
        await update.message.reply_text("❌ Invalid channel ID")


async def clear_channel_caption(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /clear_channel_caption <channel_id>")
        return

    try:
        channel_id = int(context.args[0])
        if channel_id in CHANNEL_DEFAULT_CAPTIONS:
            del CHANNEL_DEFAULT_CAPTIONS[channel_id]
            save_data()
            await update.message.reply_text("✅ Caption cleared")
        else:
            await update.message.reply_text("⚠️ No caption set")
    except:
        await update.message.reply_text("❌ Invalid channel ID")


async def enable_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /enable_autopost <channel_id>")
        return

    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not managed")
            return

        AUTO_POST_ENABLED[channel_id] = True
        save_data()

        scheduler.add_job(auto_post_job,
                         trigger=CronTrigger(minute='*/15'),
                         args=[context.bot, channel_id],
                         id=f'autopost_{channel_id}',
                         replace_existing=True)

        await update.message.reply_text(
            f"✅ Auto-post enabled for {MANAGED_CHANNELS[channel_id]['name']}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def disable_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not context.args:
        await update.message.reply_text("Usage: /disable_autopost <channel_id>")
        return

    try:
        channel_id = int(context.args[0])
        AUTO_POST_ENABLED[channel_id] = False
        save_data()

        try:
            scheduler.remove_job(f'autopost_{channel_id}')
        except:
            pass

        await update.message.reply_text(
            f"✅ Auto-post disabled for {MANAGED_CHANNELS.get(channel_id, {}).get('name', 'channel')}"
        )
    except:
        await update.message.reply_text("❌ Invalid channel ID")


async def autopost_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    text = "🤖 *Auto-Post Status:*\n\n"

    for channel_id, channel_data in MANAGED_CHANNELS.items():
        status = AUTO_POST_ENABLED.get(channel_id, False)
        images = len(CHANNEL_SPECIFIC_IMAGES.get(channel_id, []))
        text += f"• {channel_data['name']}\n"
        text += f"  Status: {'ON' if status else 'OFF'}\n"
        text += f"  Images: {images}\n\n"

    await update.message.reply_text(text, parse_mode='Markdown')


async def auto_post_job(bot, channel_id: int):
    try:
        if channel_id not in CHANNEL_SPECIFIC_IMAGES or not CHANNEL_SPECIFIC_IMAGES[
                channel_id]:
            return

        if channel_id not in CURRENT_IMAGE_INDEX:
            CURRENT_IMAGE_INDEX[channel_id] = 0

        images = CHANNEL_SPECIFIC_IMAGES[channel_id]
        index = CURRENT_IMAGE_INDEX[channel_id]
        file_id = images[index]

        caption = CHANNEL_DEFAULT_CAPTIONS.get(channel_id, DEFAULT_CAPTION)

        await bot.send_photo(channel_id, file_id, caption=caption)

        CURRENT_IMAGE_INDEX[channel_id] = (index + 1) % len(images)
        save_data()

        logger.info(f"Auto-posted to {channel_id}")
    except Exception as e:
        logger.error(f"Auto-post failed for {channel_id}: {e}")


async def export_users_report(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not USER_DATABASE:
        await update.message.reply_text("📊 No user data")
        return

    report = "USER_ID,FIRST_NAME,USERNAME,CHANNEL,STATUS,REQUEST_DATE\n"

    for user_id, user_data in USER_DATABASE.items():
        for channel_id, channel_data in user_data['channels'].items():
            report += f"{user_id},{user_data['first_name']},{user_data.get('username', 'N/A')},{channel_data['channel_name']},{channel_data['status']},{channel_data['request_date']}\n"

    file = BytesIO(report.encode('utf-8'))
    file.name = f"users_report_{datetime.now().strftime('%Y%m%d')}.csv"

    await update.message.reply_document(document=file,
                                       filename=file.name,
                                       caption="📊 User Report")


async def user_stats_command(update: Update,
                             context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    text = f"👥 *User Statistics*\n\n"
    text += f"Total Users: {len(USER_DATABASE)}\n\n"

    for channel_id, channel_data in MANAGED_CHANNELS.items():
        approved = sum(1 for u in USER_DATABASE.values()
                      if channel_id in u['channels']
                      and u['channels'][channel_id]['status'] == 'approved')
        pending = sum(1 for u in USER_DATABASE.values()
                     if channel_id in u['channels']
                     and u['channels'][channel_id]['status'] == 'pending')

        text += f"📢 {channel_data['name']}\n"
        text += f"  ✅ Approved: {approved}\n"
        text += f"  ⏳ Pending: {pending}\n\n"

    await update.message.reply_text(text, parse_mode='Markdown')


async def import_users_to_channel(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    await update.message.reply_text("Coming soon!")


async def view_unauthorized_attempts(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    if not UNAUTHORIZED_ATTEMPTS:
        await update.message.reply_text("✅ No unauthorized attempts")
        return

    text = "🚨 *Unauthorized Attempts:*\n\n"
    for attempt in UNAUTHORIZED_ATTEMPTS[-10:]:
        text += f"• User: {attempt['first_name']}\n"
        text += f"  ID: `{attempt['user_id']}`\n"
        text += f"  Command: /{attempt['command']}\n"
        text += f"  Time: {attempt['timestamp']}\n\n"

    await update.message.reply_text(text, parse_mode='Markdown')


async def clear_unauthorized_log(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    UNAUTHORIZED_ATTEMPTS.clear()
    await update.message.reply_text("✅ Unauthorized log cleared")


async def weekly_report_job(bot):
    try:
        if not USER_DATABASE:
            return

        text = "📊 *Weekly Report*\n\n"

        for channel_id, channel_data in MANAGED_CHANNELS.items():
            approved = sum(1 for u in USER_DATABASE.values()
                          if channel_id in u['channels']
                          and u['channels'][channel_id]['status'] == 'approved')

            text += f"📢 {channel_data['name']}\n"
            text += f"  New Users: {approved}\n\n"

        await bot.send_message(ADMIN_ID, text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Weekly report failed: {e}")


async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # CRITICAL FIX: Check if update has effective_user (channel posts don't have users)
    if not update.effective_user:
        logger.warning("Received update without effective_user, skipping")
        return
    
    if update.effective_user.id != ADMIN_ID:
        return

    if context.user_data.get('awaiting_code'):
        await handle_verification_code(update, context)
        return

    if context.user_data.get('uploading_mode'):
        await handle_image_upload(update, context)
        return

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
        "🎯 *Select Channel:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown')
    context.user_data['posting_mode'] = False


async def post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await query.message.reply_text(result_text, parse_mode='Markdown')


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return

    bulk_enabled = sum(1 for v in BULK_APPROVAL_MODE.values() if v)
    active_autoposts = sum(1 for v in AUTO_POST_ENABLED.values() if v)

    text = (f"📊 *Statistics*\n\n"
            f"📢 Channels: {len(MANAGED_CHANNELS)}\n"
            f"🔄 Bulk Mode: {bulk_enabled}\n"
            f"⏳ Pending: {len(PENDING_VERIFICATIONS)}\n"
            f"✅ Verified: {len(VERIFIED_FOR_CHANNELS)}\n"
            f"🚫 Blocked: {len(BLOCKED_USERS)}\n"
            f"📂 Images: {len(UPLOADED_IMAGES)}\n"
            f"🤖 Auto-Posts: {active_autoposts} active\n"
            f"👥 Total Users: {len(USER_DATABASE)}\n"
            f"🚨 Unauthorized: {len(UNAUTHORIZED_ATTEMPTS)}\n\n"
            f"Status: Online 24/7")
    await update.message.reply_text(text, parse_mode='Markdown')


# CRITICAL: Add global error handler to prevent bot crashes
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and prevent bot from crashing"""
    logger.error(f"Exception while handling an update: {context.error}")
    
    # Log the update that caused the error
    if update:
        logger.error(f"Update that caused error: {update}")
    
    # Don't let the bot crash - just log and continue
    return


def main():
    logger.info("🚀 Starting SUPER BOT...")

    # Load saved data
    load_data()

    app = Application.builder().token(BOT_TOKEN).build()

    # CRITICAL: Add error handler to prevent crashes
    app.add_error_handler(error_handler)

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

    # Join request handler
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Start scheduler
    scheduler.start()
    logger.info("✅ Scheduler started")
    
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
    logger.info(
        f"✅ Loaded: {len(MANAGED_CHANNELS)} channels, {len(UPLOADED_IMAGES)} images"
    )
    logger.info("✅ Application started")

    # Run the bot
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
