import os
import logging
from datetime import datetime
import random
import string
import re
import asyncio
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

# Unauthorized access tracking
UNAUTHORIZED_ATTEMPTS = []

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

def is_verified(user_id: int) -> bool:
    return user_id in VERIFIED_USERS or user_id == ADMIN_ID

async def is_bot_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        return bot_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
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

async def check_user_legitimacy(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    try:
        user = await context.bot.get_chat(user_id)
        score = 100
        if user.type == "bot":
            return {"legitimate": False, "reason": "Bot", "score": 0}
        if not user.first_name or is_name_suspicious(user.first_name):
            return {"legitimate": False, "reason": "Suspicious name", "score": 0}
        return {"legitimate": True, "score": score, "issues": []}
    except:
        return {"legitimate": False, "reason": "Error", "score": 0}

def track_user_activity(user_id: int, channel_id: int, action: str, user_data: dict = None):
    if user_id not in USER_DATABASE:
        USER_DATABASE[user_id] = {
            'first_name': user_data.get('first_name', 'Unknown') if user_data else 'Unknown',
            'last_name': user_data.get('last_name', ''),
            'username': user_data.get('username', ''),
            'channels': {}
        }
    if channel_id not in USER_DATABASE[user_id]['channels']:
        USER_DATABASE[user_id]['channels'][channel_id] = {
            'channel_name': MANAGED_CHANNELS.get(channel_id, {}).get('name', 'Unknown'),
            'status': action,
            'request_date': datetime.now(),
            'approval_date': None,
            'verification_attempts': 0
        }
    else:
        USER_DATABASE[user_id]['channels'][channel_id]['status'] = action
        if action == 'approved':
            USER_DATABASE[user_id]['channels'][channel_id]['approval_date'] = datetime.now()
    USER_ACTIVITY_LOG.append({
        'timestamp': datetime.now(),
        'user_id': user_id,
        'channel_id': channel_id,
        'action': action
    })

# SECURITY: Alert owner about unauthorized access
async def alert_owner_unauthorized_access(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, first_name: str, command: str):
    """Alert owner when someone tries to access the bot"""
    try:
        UNAUTHORIZED_ATTEMPTS.append({
            'user_id': user_id,
            'username': username,
            'first_name': first_name,
            'command': command,
            'timestamp': datetime.now()
        })
        
        alert_text = (
            f"🚨 *UNAUTHORIZED ACCESS ATTEMPT*\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"📝 Name: {first_name}\n"
            f"🔗 Username: @{username if username else 'None'}\n"
            f"⚡ Command: {command}\n"
            f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=alert_text,
            parse_mode='Markdown'
        )
        
        logger.warning(f"Unauthorized access attempt by {user_id} ({username})")
    except Exception as e:
        logger.error(f"Failed to send alert: {e}")

# SECURITY: Check if user is owner
async def owner_only_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is owner. If not, alert owner and block."""
    user = update.effective_user
    
    if user.id != ADMIN_ID:
        # Get command attempted
        command = update.message.text if update.message else "callback"
        
        # Alert owner
        await alert_owner_unauthorized_access(
            context,
            user.id,
            user.username,
            user.first_name,
            command
        )
        
        # Don't reply to unauthorized user - complete silence
        return False
    
    return True

async def view_unauthorized_attempts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner can view all unauthorized access attempts"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not UNAUTHORIZED_ATTEMPTS:
        await update.message.reply_text("✅ No unauthorized attempts")
        return
    
    text = "🚨 *Unauthorized Access Log*\n\n"
    
    # Show last 20 attempts
    for attempt in UNAUTHORIZED_ATTEMPTS[-20:]:
        text += (
            f"👤 `{attempt['user_id']}`\n"
            f"Name: {attempt['first_name']}\n"
            f"@{attempt['username'] if attempt['username'] else 'No username'}\n"
            f"Command: {attempt['command']}\n"
            f"Time: {attempt['timestamp'].strftime('%m-%d %H:%M')}\n\n"
        )
    
    if len(UNAUTHORIZED_ATTEMPTS) > 20:
        text += f"...and {len(UNAUTHORIZED_ATTEMPTS) - 20} more attempts\n"
    
    text += f"\nTotal: {len(UNAUTHORIZED_ATTEMPTS)} attempts"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def clear_unauthorized_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear unauthorized attempts log"""
    global UNAUTHORIZED_ATTEMPTS
    if update.effective_user.id != ADMIN_ID:
        return
    UNAUTHORIZED_ATTEMPTS = []
    await update.message.reply_text("🗑️ *Log cleared*", parse_mode='Markdown')

async def export_users_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    channel_filter = None
    if context.args:
        try:
            channel_filter = int(context.args[0])
        except:
            pass
    msg = await update.message.reply_text("⏳ Generating report...")
    csv_lines = ["User ID,First Name,Last Name,Username,Channel Name,Channel ID,Status,Request Date,Approval Date\n"]
    for user_id, user_data in USER_DATABASE.items():
        for channel_id, channel_data in user_data['channels'].items():
            if channel_filter and channel_id != channel_filter:
                continue
            csv_lines.append(
                f"{user_id},"
                f'"{user_data["first_name"]}",'
                f'"{user_data["last_name"]}",'
                f'"{user_data["username"]}",'
                f'"{channel_data["channel_name"]}",'
                f"{channel_id},"
                f"{channel_data['status']},"
                f"{channel_data['request_date'].strftime('%Y-%m-%d %H:%M')},"
                f"{channel_data['approval_date'].strftime('%Y-%m-%d %H:%M') if channel_data['approval_date'] else 'N/A'}\n"
            )
    if len(csv_lines) == 1:
        await msg.edit_text("❌ No user data to export")
        return
    csv_content = ''.join(csv_lines)
    filename = f"user_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    file_data = BytesIO(csv_content.encode('utf-8-sig'))
    file_data.name = filename
    await context.bot.send_document(
        chat_id=ADMIN_ID,
        document=file_data,
        filename=filename,
        caption=f"📊 *User Report*\n\nTotal Users: {len(USER_DATABASE)}\nTotal Records: {len(csv_lines)-1}",
        parse_mode='Markdown'
    )
    await msg.delete()

async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Generating weekly user report...")
    csv_lines = ["User ID,First Name,Last Name,Username,Channel Name,Channel ID,Status,Request Date,Approval Date\n"]
    for user_id, user_data in USER_DATABASE.items():
        for channel_id, channel_data in user_data['channels'].items():
            csv_lines.append(
                f"{user_id},"
                f'"{user_data["first_name"]}",'
                f'"{user_data["last_name"]}",'
                f'"{user_data["username"]}",'
                f'"{channel_data["channel_name"]}",'
                f"{channel_id},"
                f"{channel_data['status']},"
                f"{channel_data['request_date'].strftime('%Y-%m-%d %H:%M')},"
                f"{channel_data['approval_date'].strftime('%Y-%m-%d %H:%M') if channel_data['approval_date'] else 'N/A'}\n"
            )
    if len(csv_lines) > 1:
        csv_content = ''.join(csv_lines)
        filename = f"weekly_report_{datetime.now().strftime('%Y%m%d')}.csv"
        file_data = BytesIO(csv_content.encode('utf-8-sig'))
        file_data.name = filename
        try:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=file_data,
                filename=filename,
                caption=f"📊 *Weekly User Report*\n\nTotal Users: {len(USER_DATABASE)}\nTotal Records: {len(csv_lines)-1}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to send weekly report: {e}")

async def user_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    text = "📊 *User Statistics*\n\n"
    for channel_id, channel_data in MANAGED_CHANNELS.items():
        pending = 0
        approved = 0
        rejected = 0
        for user_id, user_data in USER_DATABASE.items():
            if channel_id in user_data['channels']:
                status = user_data['channels'][channel_id]['status']
                if status == 'pending':
                    pending += 1
                elif status == 'approved':
                    approved += 1
                elif status == 'rejected':
                    rejected += 1
        text += (
            f"*{channel_data['name']}*\n"
            f"✅ Approved: {approved}\n"
            f"⏳ Pending: {pending}\n"
            f"❌ Rejected: {rejected}\n\n"
        )
    text += f"📁 *Total Unique Users:* {len(USER_DATABASE)}"
    await update.message.reply_text(text, parse_mode='Markdown')

async def import_users_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args:
        await update.message.reply_text(
            "📥 *Import Users*\n\n"
            "*Usage:*\n"
            "`/import_users TARGET_CHANNEL_ID`\n"
            "or\n"
            "`/import_users TARGET_CHANNEL_ID SOURCE_CHANNEL_ID`",
            parse_mode='Markdown'
        )
        return
    try:
        target_channel_id = int(context.args[0])
        source_channel_id = int(context.args[1]) if len(context.args) > 1 else None
        if target_channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Target channel not found")
            return
        if not await is_bot_admin(context, target_channel_id):
            await update.message.reply_text("❌ Bot must be admin in target channel")
            return
        try:
            invite_link = await context.bot.export_chat_invite_link(target_channel_id)
        except:
            await update.message.reply_text("❌ Cannot create invite link")
            return
        users_to_import = []
        for user_id, user_data in USER_DATABASE.items():
            if user_id in BLOCKED_USERS:
                continue
            if target_channel_id in user_data['channels']:
                continue
            if source_channel_id:
                if source_channel_id in user_data['channels'] and user_data['channels'][source_channel_id]['status'] == 'approved':
                    users_to_import.append(user_id)
            else:
                has_approved = any(ch['status'] == 'approved' for ch in user_data['channels'].values())
                if has_approved:
                    users_to_import.append(user_id)
        if not users_to_import:
            await update.message.reply_text("📭 No users to import")
            return
        msg = await update.message.reply_text(f"⏳ Importing {len(users_to_import)} users...")
        success = 0
        failed = 0
        already_member = 0
        for user_id in users_to_import:
            try:
                try:
                    member = await context.bot.get_chat_member(target_channel_id, user_id)
                    if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                        already_member += 1
                        continue
                except:
                    pass
                try:
                    await context.bot.send_message(
                        user_id,
                        f"🎉 *You're invited!*\n\nJoin: *{MANAGED_CHANNELS[target_channel_id]['name']}*\n\n{invite_link}",
                        parse_mode='Markdown'
                    )
                    success += 1
                    user_info = USER_DATABASE[user_id]
                    track_user_activity(user_id, target_channel_id, 'invited', {
                        'first_name': user_info['first_name'],
                        'last_name': user_info['last_name'],
                        'username': user_info['username']
                    })
                except:
                    failed += 1
                if (success + failed) % 50 == 0:
                    await msg.edit_text(f"⏳ Progress: {success + failed}/{len(users_to_import)}\n✅ Sent: {success}\n❌ Failed: {failed}")
                await asyncio.sleep(0.1)
            except:
                failed += 1
        await msg.edit_text(
            f"✅ *Import Complete!*\n\n"
            f"Target: {MANAGED_CHANNELS[target_channel_id]['name']}\n"
            f"Total: {len(users_to_import)}\n"
            f"✅ Invited: {success}\n"
            f"👥 Already member: {already_member}\n"
            f"❌ Failed: {failed}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def set_default_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DEFAULT_CAPTION
    if not await owner_only_check(update, context):
        return
    if not context.args:
        current = DEFAULT_CAPTION if DEFAULT_CAPTION else "None"
        await update.message.reply_text(
            f"📝 *Default Caption*\n\nCurrent: {current}\n\n"
            f"*Set:* `/set_default_caption Text`\n"
            f"*Clear:* `/clear_default_caption`",
            parse_mode='Markdown'
        )
        return
    caption = " ".join(context.args)
    DEFAULT_CAPTION = caption
    await update.message.reply_text(f"✅ *Default Caption Set!*\n\n{caption}", parse_mode='Markdown')

async def clear_default_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DEFAULT_CAPTION
    if not await owner_only_check(update, context):
        return
    DEFAULT_CAPTION = ""
    await update.message.reply_text("🗑️ *Default caption cleared*", parse_mode='Markdown')

async def set_channel_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args or len(context.args) < 2:
        text = "📝 *Set Channel Caption*\n\n*Usage:* `/set_channel_caption CHANNEL_ID Text`"
        await update.message.reply_text(text, parse_mode='Markdown')
        return
    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not found")
            return
        caption = " ".join(context.args[1:])
        CHANNEL_DEFAULT_CAPTIONS[channel_id] = caption
        await update.message.reply_text(
            f"✅ *Channel Caption Set!*\n\nChannel: {MANAGED_CHANNELS[channel_id]['name']}\nCaption: {caption}",
            parse_mode='Markdown'
        )
    except:
        await update.message.reply_text("❌ Invalid channel ID")

async def clear_channel_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/clear_channel_caption CHANNEL_ID`", parse_mode='Markdown')
        return
    try:
        channel_id = int(context.args[0])
        if channel_id in CHANNEL_DEFAULT_CAPTIONS:
            del CHANNEL_DEFAULT_CAPTIONS[channel_id]
            await update.message.reply_text("🗑️ *Channel caption cleared*", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ No caption set for this channel")
    except:
        await update.message.reply_text("❌ Invalid channel ID")

async def auto_post_job(context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    if not AUTO_POST_ENABLED.get(channel_id):
        return
    if channel_id in CHANNEL_SPECIFIC_IMAGES and CHANNEL_SPECIFIC_IMAGES[channel_id]:
        images = CHANNEL_SPECIFIC_IMAGES[channel_id]
    elif UPLOADED_IMAGES:
        images = UPLOADED_IMAGES
    else:
        return
    if channel_id not in CURRENT_IMAGE_INDEX:
        CURRENT_IMAGE_INDEX[channel_id] = 0
    idx = CURRENT_IMAGE_INDEX[channel_id]
    image = images[idx]
    if image.get('caption'):
        final_caption = image['caption']
    elif channel_id in CHANNEL_DEFAULT_CAPTIONS:
        final_caption = CHANNEL_DEFAULT_CAPTIONS[channel_id]
    elif DEFAULT_CAPTION:
        final_caption = DEFAULT_CAPTION
    else:
        final_caption = ""
    try:
        await context.bot.send_photo(
            chat_id=channel_id,
            photo=image['file_id'],
            caption=final_caption
        )
        logger.info(f"Posted to {channel_id}")
        CURRENT_IMAGE_INDEX[channel_id] = (idx + 1) % len(images)
    except Exception as e:
        logger.error(f"Post failed: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    
    user_id = update.effective_user.id
    VERIFIED_USERS.add(user_id)
    settings_text = (
        f"Age: {MIN_ACCOUNT_AGE_DAYS} days\n"
        f"Photo: {'Required' if REQUIRE_PROFILE_PHOTO else 'Optional'}\n"
        f"Code: {CODE_EXPIRY_MINUTES} mins\n"
        f"Name: Strict"
    )
    await update.message.reply_text(
        "🎯 *SUPER BOT - OWNER PANEL*\n\n"
        "*📢 Channel:*\n"
        "/addchannel /channels /toggle\\_bulk\n\n"
        "*👥 Users:*\n"
        "/pending\\_users /approve\\_user /approve\\_all\\_pending\n"
        "/bulk\\_approve /block\\_user /unblock\\_user\n\n"
        "*📤 Content:*\n"
        "/post /upload\\_images /upload\\_for\\_channel\n"
        "/list\\_images /clear\\_images\n\n"
        "*📝 Captions:*\n"
        "/set\\_default\\_caption /set\\_channel\\_caption\n\n"
        "*🤖 Auto-Post:*\n"
        "/enable\\_autopost /disable\\_autopost /autopost\\_status\n\n"
        "*📊 Reports:*\n"
        "/export\\_users /user\\_stats /import\\_users\n\n"
        "*🚨 Security:*\n"
        "/view\\_unauthorized /clear\\_unauthorized\n\n"
        "*📊 Stats:* /stats\n\n"
        f"🔒 *Settings:*\n{settings_text}",
        parse_mode='Markdown'
    )

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_join_request:
        return
    user_id = update.chat_join_request.from_user.id
    channel_id = update.chat_join_request.chat.id
    channel_name = MANAGED_CHANNELS.get(channel_id, {}).get("name", "Unknown")
    user = update.chat_join_request.from_user
    track_user_activity(user_id, channel_id, 'pending', {
        'first_name': user.first_name,
        'last_name': user.last_name,
        'username': user.username
    })
    if BULK_APPROVAL_MODE.get(channel_id, False):
        try:
            await context.bot.approve_chat_join_request(channel_id, user_id)
            track_user_activity(user_id, channel_id, 'approved')
            return
        except:
            return
    if user_id in BLOCKED_USERS:
        try:
            await context.bot.decline_chat_join_request(channel_id, user_id)
            track_user_activity(user_id, channel_id, 'rejected')
        except:
            pass
        return
    legitimacy_check = await check_user_legitimacy(context, user_id)
    if not legitimacy_check["legitimate"]:
        try:
            await context.bot.decline_chat_join_request(channel_id, user_id)
            track_user_activity(user_id, channel_id, 'rejected')
        except:
            pass
        return
    verification_code = generate_verification_code()
    PENDING_VERIFICATIONS[user_id] = {
        'channel_id': channel_id,
        'channel_name': channel_name,
        'code': verification_code,
        'timestamp': datetime.now(),
        'attempts': 0,
        'max_attempts': 3
    }
    try:
        keyboard = [
            [InlineKeyboardButton("✅ Enter Code", callback_data=f"enter_code_{user_id}")],
            [InlineKeyboardButton("🔄 Resend", callback_data=f"resend_code_{user_id}")]
        ]
        await context.bot.send_message(
            user_id,
            f"🔐 *Verification Required*\n\nChannel: *{channel_name}*\n\nCode:\n```\n{verification_code}\n```\n\n⏱️ {CODE_EXPIRY_MINUTES} mins",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    except:
        pass

async def enter_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in PENDING_VERIFICATIONS:
        await query.edit_message_text("❌ No pending verification")
        return
    await query.edit_message_text("📝 *Enter Code*\n\nReply with your code.", parse_mode='Markdown')
    context.user_data['awaiting_code'] = True

async def resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Resending...")
    user_id = query.from_user.id
    if user_id not in PENDING_VERIFICATIONS:
        await query.edit_message_text("❌ No pending verification")
        return
    verification = PENDING_VERIFICATIONS[user_id]
    new_code = generate_verification_code()
    verification['code'] = new_code
    verification['timestamp'] = datetime.now()
    keyboard = [
        [InlineKeyboardButton("✅ Enter Code", callback_data=f"enter_code_{user_id}")],
        [InlineKeyboardButton("🔄 Resend", callback_data=f"resend_code_{user_id}")]
    ]
    await query.edit_message_text(
        f"🔐 *New Code*\n\n```\n{new_code}\n```\n\n⏱️ {CODE_EXPIRY_MINUTES} mins",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def handle_verification_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_code'):
        return
    user_id = update.effective_user.id
    submitted_code = update.message.text.strip().upper()
    if user_id not in PENDING_VERIFICATIONS:
        context.user_data['awaiting_code'] = False
        return
    verification = PENDING_VERIFICATIONS[user_id]
    if (datetime.now() - verification['timestamp']).seconds > (CODE_EXPIRY_MINUTES * 60):
        del PENDING_VERIFICATIONS[user_id]
        context.user_data['awaiting_code'] = False
        await update.message.reply_text("❌ *Code Expired*", parse_mode='Markdown')
        try:
            await context.bot.decline_chat_join_request(verification['channel_id'], user_id)
            track_user_activity(user_id, verification['channel_id'], 'rejected')
        except:
            pass
        return
    verification['attempts'] += 1
    if submitted_code == verification['code']:
        context.user_data['awaiting_code'] = False
        try:
            await context.bot.approve_chat_join_request(verification['channel_id'], user_id)
            track_user_activity(user_id, verification['channel_id'], 'approved')
            await update.message.reply_text(f"✅ *Verified!*\n\nWelcome to *{verification['channel_name']}*! 🎉", parse_mode='Markdown')
            if user_id not in VERIFIED_FOR_CHANNELS:
                VERIFIED_FOR_CHANNELS[user_id] = []
            VERIFIED_FOR_CHANNELS[user_id].append(verification['channel_id'])
            del PENDING_VERIFICATIONS[user_id]
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
    else:
        remaining = verification['max_attempts'] - verification['attempts']
        if remaining > 0:
            await update.message.reply_text(f"❌ *Incorrect*\n\nAttempts left: {remaining}", parse_mode='Markdown')
        else:
            del PENDING_VERIFICATIONS[user_id]
            context.user_data['awaiting_code'] = False
            BLOCKED_USERS.add(user_id)
            await update.message.reply_text("❌ *Failed*\n\nBlocked.", parse_mode='Markdown')
            try:
                await context.bot.decline_chat_join_request(verification['channel_id'], user_id)
                track_user_activity(user_id, verification['channel_id'], 'rejected')
            except:
                pass

async def upload_images_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    await update.message.reply_text("📤 *Upload Images*\n\nSend photos", parse_mode='Markdown')
    context.user_data['uploading_mode'] = 'general'

async def upload_for_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args:
        text = "📢 *Upload for Channel*\n\n"
        for cid, data in MANAGED_CHANNELS.items():
            text += f"{data['name']}: `{cid}`\n"
        await update.message.reply_text(text, parse_mode='Markdown')
        return
    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Invalid channel")
            return
        context.user_data['uploading_mode'] = 'channel_specific'
        context.user_data['upload_channel_id'] = channel_id
        await update.message.reply_text(f"📤 Send images for *{MANAGED_CHANNELS[channel_id]['name']}*", parse_mode='Markdown')
    except:
        await update.message.reply_text("❌ Invalid ID")

async def handle_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not update.message.photo:
        return
    uploading_mode = context.user_data.get('uploading_mode')
    if not uploading_mode:
        return
    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    image_data = {'file_id': photo.file_id, 'caption': caption}
    if uploading_mode == 'general':
        UPLOADED_IMAGES.append(image_data)
        await update.message.reply_text(f"✅ {len(UPLOADED_IMAGES)}")
    elif uploading_mode == 'channel_specific':
        channel_id = context.user_data.get('upload_channel_id')
        if channel_id:
            if channel_id not in CHANNEL_SPECIFIC_IMAGES:
                CHANNEL_SPECIFIC_IMAGES[channel_id] = []
            CHANNEL_SPECIFIC_IMAGES[channel_id].append(image_data)
            await update.message.reply_text(f"✅ {len(CHANNEL_SPECIFIC_IMAGES[channel_id])}")

async def list_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    text = f"📂 *General:* {len(UPLOADED_IMAGES)}\n\n"
    for channel_id, images in CHANNEL_SPECIFIC_IMAGES.items():
        text += f"{MANAGED_CHANNELS.get(channel_id, {}).get('name', 'Unknown')}: {len(images)}\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def clear_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global UPLOADED_IMAGES
    if not await owner_only_check(update, context):
        return
    UPLOADED_IMAGES = []
    await update.message.reply_text("🗑️ Cleared")

async def enable_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/enable_autopost CHANNEL_ID`", parse_mode='Markdown')
        return
    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not found")
            return
        has_images = UPLOADED_IMAGES or (channel_id in CHANNEL_SPECIFIC_IMAGES and CHANNEL_SPECIFIC_IMAGES[channel_id])
        if not has_images:
            await update.message.reply_text("❌ Upload images first")
            return
        AUTO_POST_ENABLED[channel_id] = True
        scheduler.add_job(auto_post_job, trigger=CronTrigger(hour='*'), args=[context, channel_id], id=f'autopost_{channel_id}', replace_existing=True)
        await update.message.reply_text(f"🚀 *Enabled!*\n\n{MANAGED_CHANNELS[channel_id]['name']}", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def disable_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args:
        return
    try:
        channel_id = int(context.args[0])
        AUTO_POST_ENABLED[channel_id] = False
        try:
            scheduler.remove_job(f'autopost_{channel_id}')
        except:
            pass
        await update.message.reply_text("⏹️ Stopped", parse_mode='Markdown')
    except:
        await update.message.reply_text("❌ Invalid ID")

async def autopost_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    text = "🤖 *Status*\n\n"
    for channel_id, data in MANAGED_CHANNELS.items():
        status = "🟢" if AUTO_POST_ENABLED.get(channel_id) else "🔴"
        if channel_id in CHANNEL_SPECIFIC_IMAGES:
            count = len(CHANNEL_SPECIFIC_IMAGES[channel_id])
        else:
            count = len(UPLOADED_IMAGES)
        idx = CURRENT_IMAGE_INDEX.get(channel_id, 0)
        text += f"{status} *{data['name']}* - {count} imgs - Next: #{idx+1}\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def pending_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not PENDING_VERIFICATIONS:
        await update.message.reply_text("📭 None")
        return
    text = "⏳ *Pending:*\n\n"
    for user_id, data in list(PENDING_VERIFICATIONS.items())[:20]:
        text += f"`{user_id}` - {data['channel_name']}\n"
    text += f"\nTotal: {len(PENDING_VERIFICATIONS)}"
    await update.message.reply_text(text, parse_mode='Markdown')

async def manual_approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args or len(context.args) < 2:
        return
    try:
        user_id = int(context.args[0])
        channel_id = int(context.args[1])
        await context.bot.approve_chat_join_request(channel_id, user_id)
        track_user_activity(user_id, channel_id, 'approved')
        await update.message.reply_text(f"✅ Approved")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def approve_all_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not PENDING_VERIFICATIONS:
        return
    msg = await update.message.reply_text(f"⏳ Approving...")
    approved = 0
    for user_id, data in dict(PENDING_VERIFICATIONS).items():
        try:
            await context.bot.approve_chat_join_request(data['channel_id'], user_id)
            track_user_activity(user_id, data['channel_id'], 'approved')
            del PENDING_VERIFICATIONS[user_id]
            approved += 1
        except:
            pass
        await asyncio.sleep(0.1)
    await msg.edit_text(f"✅ Done: {approved}")

async def bulk_approve_from_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    await update.message.reply_text("📄 Upload file with caption: `/bulk_approve CHANNEL_ID`", parse_mode='Markdown')

async def handle_bulk_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not update.message.document:
        return
    caption = update.message.caption or ""
    if not caption.startswith("/bulk_approve"):
        return
    try:
        parts = caption.split()
        channel_id = int(parts[1])
        file = await context.bot.get_file(update.message.document.file_id)
        file_content = await file.download_as_bytearray()
        user_ids = [int(line.strip()) for line in file_content.decode('utf-8').split('\n') if line.strip().isdigit()]
        msg = await update.message.reply_text(f"⏳ Processing...")
        approved = 0
        for user_id in user_ids:
            try:
                await context.bot.approve_chat_join_request(channel_id, user_id)
                approved += 1
            except:
                pass
            await asyncio.sleep(0.05)
        await msg.edit_text(f"✅ Done: {approved}/{len(user_ids)}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args:
        return
    user_id = int(context.args[0])
    BLOCKED_USERS.add(user_id)
    await update.message.reply_text(f"🚫 Blocked")

async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args:
        return
    user_id = int(context.args[0])
    if user_id in BLOCKED_USERS:
        BLOCKED_USERS.remove(user_id)
        await update.message.reply_text("✅ Unblocked")

async def verification_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    text = f"🔒 *Settings*\n\nAge: {MIN_ACCOUNT_AGE_DAYS}\nExpiry: {CODE_EXPIRY_MINUTES} mins"
    await update.message.reply_text(text, parse_mode='Markdown')

async def toggle_bulk_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not context.args:
        text = "🔄 *Bulk Mode*\n\n"
        for cid, data in MANAGED_CHANNELS.items():
            status = "ON" if BULK_APPROVAL_MODE.get(cid) else "OFF"
            text += f"{data['name']}: {status}\n"
        await update.message.reply_text(text, parse_mode='Markdown')
        return
    try:
        channel_id = int(context.args[0])
        BULK_APPROVAL_MODE[channel_id] = not BULK_APPROVAL_MODE.get(channel_id, False)
        status = "ON" if BULK_APPROVAL_MODE[channel_id] else "OFF"
        await update.message.reply_text(f"🔄 {status}")
    except:
        await update.message.reply_text("❌ Invalid")

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    await update.message.reply_text("📢 Add bot as admin, then forward message")

async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if update.message.forward_from_chat:
        channel = update.message.forward_from_chat
        if channel.type in ['channel', 'supergroup']:
            if not await is_bot_admin(context, channel.id):
                await update.message.reply_text("❌ Make me ADMIN")
                return
            MANAGED_CHANNELS[channel.id] = {"name": channel.title}
            BULK_APPROVAL_MODE[channel.id] = False
            await update.message.reply_text(f"✅ {channel.title}\n`{channel.id}`", parse_mode='Markdown')

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    if not MANAGED_CHANNELS:
        await update.message.reply_text("📭 None")
        return
    text = "📢 *Channels:*\n\n"
    for cid, data in MANAGED_CHANNELS.items():
        text += f"*{data['name']}*\n`{cid}`\n\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    await update.message.reply_text("📤 Send content")
    context.user_data['posting_mode'] = True

async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    PENDING_POSTS[ADMIN_ID] = {'message': message, 'type': content_type}
    keyboard = []
    for cid, data in MANAGED_CHANNELS.items():
        keyboard.append([InlineKeyboardButton(data['name'], callback_data=f"post_{cid}")])
    keyboard.append([InlineKeyboardButton("ALL", callback_data="post_all")])
    await update.message.reply_text("Select:", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['posting_mode'] = False

async def post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Unauthorized", show_alert=True)
        return
    await query.answer()
    action = query.data.split('_')[1]
    if action == "cancel":
        await query.edit_message_text("❌ Cancelled")
        return
    pending = PENDING_POSTS[ADMIN_ID]
    msg = pending['message']
    channels = [int(action)] if action != "all" else list(MANAGED_CHANNELS.keys())
    success = 0
    for cid in channels:
        try:
            if pending['type'] == 'text':
                await context.bot.send_message(cid, msg.text)
            elif pending['type'] == 'photo':
                await context.bot.send_photo(cid, msg.photo[-1].file_id, caption=msg.caption)
            success += 1
        except:
            pass
    del PENDING_POSTS[ADMIN_ID]
    await query.edit_message_text(f"✅ Posted: {success}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only_check(update, context):
        return
    text = (
        f"📊 *Stats*\n\n"
        f"Channels: {len(MANAGED_CHANNELS)}\n"
        f"Pending: {len(PENDING_VERIFICATIONS)}\n"
        f"Blocked: {len(BLOCKED_USERS)}\n"
        f"Images: {len(UPLOADED_IMAGES)}\n"
        f"Users: {len(USER_DATABASE)}\n"
        f"Unauthorized: {len(UNAUTHORIZED_ATTEMPTS)}"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

def main():
    logger.info("🚀 Starting OWNER-ONLY bot...")
    app = Application.builder().token(BOT_TOKEN).build()
    
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
    app.add_handler(CommandHandler("verification_settings", verification_settings))
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("upload_images", upload_images_command))
    app.add_handler(CommandHandler("upload_for_channel", upload_for_channel_command))
    app.add_handler(CommandHandler("list_images", list_images))
    app.add_handler(CommandHandler("clear_images", clear_images))
    app.add_handler(CommandHandler("set_default_caption", set_default_caption))
    app.add_handler(CommandHandler("clear_default_caption", clear_default_caption))
    app.add_handler(CommandHandler("set_channel_caption", set_channel_caption))
    app.add_handler(CommandHandler("clear_channel_caption", clear_channel_caption))
    app.add_handler(CommandHandler("enable_autopost", enable_autopost))
    app.add_handler(CommandHandler("disable_autopost", disable_autopost))
    app.add_handler(CommandHandler("autopost_status", autopost_status))
    app.add_handler(CommandHandler("export_users", export_users_report))
    app.add_handler(CommandHandler("user_stats", user_stats_command))
    app.add_handler(CommandHandler("import_users", import_users_to_channel))
    app.add_handler(CommandHandler("view_unauthorized", view_unauthorized_attempts))
    app.add_handler(CommandHandler("clear_unauthorized", clear_unauthorized_log))
    app.add_handler(CommandHandler("stats", stats))
    
    app.add_handler(CallbackQueryHandler(enter_code_callback, pattern="^enter_code_"))
    app.add_handler(CallbackQueryHandler(resend_code_callback, pattern="^resend_code_"))
    app.add_handler(CallbackQueryHandler(post_callback, pattern="^post_"))
    
    app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, handle_forwarded_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_bulk_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_content))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_content))
    
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    
    scheduler.start()
    scheduler.add_job(weekly_report_job, trigger=CronTrigger(day_of_week='mon', hour=9), args=[app.bot], id='weekly_report')
    
    logger.info(f"✅ OWNER-ONLY mode active - Admin: {ADMIN_ID}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
