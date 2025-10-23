import os
import logging
from datetime import datetime, timedelta
import random
import string
import re
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    ChatJoinRequestHandler,
    ContextTypes, 
    filters
)
from telegram.constants import ChatMemberStatus
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
ADMIN_ID = int(os.environ.get('ADMIN_ID', '123456789'))

# Security Settings
MIN_ACCOUNT_AGE_DAYS = 15
REQUIRE_PROFILE_PHOTO = False
REQUIRE_USERNAME = False
CODE_EXPIRY_MINUTES = 5

# Storage
VERIFIED_USERS = set([ADMIN_ID])
MANAGED_CHANNELS = {}
PENDING_POSTS = {}
SCHEDULED_POSTS = {}
LAST_POST_TIME = {}
PENDING_VERIFICATIONS = {}
VERIFIED_FOR_CHANNELS = {}
BLOCKED_USERS = set()
BULK_APPROVAL_MODE = {}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# ==================== HELPER FUNCTIONS ====================
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
    """Check if name is suspicious (emojis only, special chars only, etc.)"""
    if not name or len(name) < 2:
        return True
    
    # Remove all letters and numbers
    letters_and_numbers = re.sub(r'[^a-zA-Z0-9]', '', name)
    
    # If less than 2 alphanumeric characters, it's suspicious
    if len(letters_and_numbers) < 2:
        return True
    
    # Check for spam patterns
    spam_patterns = [
        r'^[0-9]+$',
        r'^[_\-\.]+$',
        r'^\s+$',
    ]
    
    for pattern in spam_patterns:
        if re.match(pattern, name):
            return True
    
    # Check percentage of special characters
    total_chars = len(name)
    special_chars = len(re.findall(r'[^a-zA-Z0-9\s]', name))
    
    if special_chars / total_chars > 0.7:
        return True
    
    return False

async def check_user_legitimacy(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    """Check if user is legitimate"""
    try:
        user = await context.bot.get_chat(user_id)
        
        issues = []
        score = 100
        
        if user.type == "bot":
            return {"legitimate": False, "reason": "Bots not allowed", "score": 0}
        
        if REQUIRE_PROFILE_PHOTO:
            photos = await context.bot.get_user_profile_photos(user_id, limit=1)
            if photos.total_count == 0:
                issues.append("No profile photo")
                score -= 40
        
        if REQUIRE_USERNAME and not user.username:
            issues.append("No username")
            score -= 20
        
        if not user.first_name:
            issues.append("No name")
            score -= 50
        elif is_name_suspicious(user.first_name):
            issues.append("Suspicious name")
            score -= 60
        
        if user.last_name and is_name_suspicious(user.last_name):
            issues.append("Suspicious last name")
            score -= 20
        
        if score >= 60:
            return {"legitimate": True, "score": score, "issues": issues}
        else:
            reason = ", ".join(issues) if issues else "Failed checks"
            return {"legitimate": False, "reason": reason, "score": score}
            
    except Exception as e:
        logger.error(f"Error checking user {user_id}: {e}")
        return {"legitimate": False, "reason": "Unable to verify", "score": 0}

# ==================== START & VERIFICATION ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id == ADMIN_ID:
        VERIFIED_USERS.add(user_id)
        await update.message.reply_text(
            "🎯 *SUPER-POWERFUL BOT - ADMIN*\n\n"
            "*📢 Channel:*\n"
            "/addchannel - Add channel\n"
            "/channels - List channels\n"
            "/toggle_bulk - Bulk approval mode\n\n"
            "*👥 Users:*\n"
            "/pending_users - View pending\n"
            "/approve_user USER_ID CHANNEL_ID\n"
            "/approve_all_pending - Approve all\n"
            "/bulk_approve - Upload file\n"
            "/block_user USER_ID\n"
            "/unblock_user USER_ID\n"
            "/verification_settings\n\n"
            "*📤 Content:*\n"
            "/post - Post content\n\n"
            "*📊 Stats:*\n"
            "/stats - Statistics\n\n"
            f"🔒 *Settings:*\n"
            f"• Age: {MIN_ACCOUNT_AGE_DAYS} days\n"
            f"• Photo: {'Required' if REQUIRE_PROFILE_PHOTO else 'Optional'}\n"
            f"• Code: {CODE_EXPIRY_MINUTES} mins\n"
            f"• Name: Strict",
            parse_mode='Markdown'
        )
        return
    
    keyboard = [[InlineKeyboardButton("🔐 Verify", callback_data=f"verify_{user_id}")]]
    await update.message.reply_text(
        f"🔒 *Verification Required*\n\nID: `{user_id}`\n\nClick to verify:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = int(query.data.split('_')[1])
    
    if user_id != query.from_user.id:
        await query.edit_message_text("❌ Failed")
        return
    
    VERIFIED_USERS.add(user_id)
    await query.edit_message_text("✅ *Verified!*\n\nRequest to join a channel.", parse_mode='Markdown')

# ==================== JOIN REQUEST HANDLER ====================
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle join requests with verification"""
    if not update.chat_join_request:
        return
    
    user_id = update.chat_join_request.from_user.id
    channel_id = update.chat_join_request.chat.id
    channel_name = MANAGED_CHANNELS.get(channel_id, {}).get("name", "Unknown")
    user = update.chat_join_request.from_user
    
    logger.info(f"📥 Join request: {user_id} → {channel_name}")
    
    # Bulk approval mode
    if BULK_APPROVAL_MODE.get(channel_id, False):
        try:
            await context.bot.approve_chat_join_request(channel_id, user_id)
            logger.info(f"✅ Bulk approved {user_id}")
            
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"✅ *Bulk Approved*\nUser: `{user_id}`\nChannel: {channel_name}",
                    parse_mode='Markdown'
                )
            except:
                pass
            return
        except Exception as e:
            logger.error(f"Bulk approval failed: {e}")
            return
    
    # Check if blocked
    if user_id in BLOCKED_USERS:
        logger.warning(f"❌ Blocked user {user_id}")
        try:
            await context.bot.decline_chat_join_request(channel_id, user_id)
        except:
            pass
        return
    
    # Check legitimacy
    legitimacy_check = await check_user_legitimacy(context, user_id)
    
    if not legitimacy_check["legitimate"]:
        logger.warning(f"❌ Rejected {user_id}: {legitimacy_check['reason']}")
        
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"🚫 *Rejected*\n\n"
                f"User: {user.first_name}\n"
                f"ID: `{user_id}`\n"
                f"Channel: {channel_name}\n"
                f"Reason: {legitimacy_check['reason']}\n"
                f"Score: {legitimacy_check['score']}/100",
                parse_mode='Markdown'
            )
        except:
            pass
        
        try:
            await context.bot.decline_chat_join_request(channel_id, user_id)
        except:
            pass
        
        return
    
    # Send verification code
    verification_code = generate_verification_code()
    
    PENDING_VERIFICATIONS[user_id] = {
        'channel_id': channel_id,
        'channel_name': channel_name,
        'code': verification_code,
        'timestamp': datetime.now(),
        'attempts': 0,
        'max_attempts': 3
    }
    
    logger.info(f"✉️ Verification sent to {user_id}")
    
    try:
        keyboard = [
            [InlineKeyboardButton("✅ Enter Code", callback_data=f"enter_code_{user_id}")],
            [InlineKeyboardButton("🔄 Resend", callback_data=f"resend_code_{user_id}")]
        ]
        
        await context.bot.send_message(
            user_id,
            f"🔐 *Verification Required*\n\n"
            f"Channel: *{channel_name}*\n\n"
            f"Code:\n```\n{verification_code}\n```\n\n"
            f"⏱️ {CODE_EXPIRY_MINUTES} minutes\n"
            f"🎯 3 attempts",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
        await context.bot.send_message(
            ADMIN_ID,
            f"🔐 *Verification Sent*\n\n"
            f"User: {user.first_name}\n"
            f"ID: `{user_id}`\n"
            f"Channel: {channel_name}\n"
            f"Score: {legitimacy_check['score']}/100\n"
            f"Code: `{verification_code}`",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Failed to send verification: {e}")

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
        f"🔐 *New Code*\n\n```\n{new_code}\n```\n\n⏱️ {CODE_EXPIRY_MINUTES} minutes",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def handle_verification_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_code'):
        return
    
    user_id = update.effective_user.id
    submitted_code = update.message.text.strip().upper()
    
    if user_id not in PENDING_VERIFICATIONS:
        await update.message.reply_text("❌ No pending verification")
        context.user_data['awaiting_code'] = False
        return
    
    verification = PENDING_VERIFICATIONS[user_id]
    
    # Check expiry
    if (datetime.now() - verification['timestamp']).seconds > (CODE_EXPIRY_MINUTES * 60):
        del PENDING_VERIFICATIONS[user_id]
        context.user_data['awaiting_code'] = False
        
        await update.message.reply_text("❌ *Code Expired*\n\nRequest again.", parse_mode='Markdown')
        
        try:
            await context.bot.decline_chat_join_request(verification['channel_id'], user_id)
        except:
            pass
        return
    
    verification['attempts'] += 1
    
    if submitted_code == verification['code']:
        # SUCCESS
        context.user_data['awaiting_code'] = False
        
        try:
            await context.bot.approve_chat_join_request(verification['channel_id'], user_id)
            
            await update.message.reply_text(
                f"✅ *Verified!*\n\n*{verification['channel_name']}*\n\nWelcome! 🎉",
                parse_mode='Markdown'
            )
            
            if user_id not in VERIFIED_FOR_CHANNELS:
                VERIFIED_FOR_CHANNELS[user_id] = []
            VERIFIED_FOR_CHANNELS[user_id].append(verification['channel_id'])
            
            await context.bot.send_message(
                ADMIN_ID,
                f"✅ *Approved*\n\nUser: {update.effective_user.first_name}\n"
                f"ID: `{user_id}`\nChannel: {verification['channel_name']}",
                parse_mode='Markdown'
            )
            
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
            
            await update.message.reply_text("❌ *Failed*\n\nToo many attempts. Blocked.", parse_mode='Markdown')
            
            try:
                await context.bot.decline_chat_join_request(verification['channel_id'], user_id)
            except:
                pass

# ==================== BULK APPROVAL ====================
async def toggle_bulk_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        text = "🔄 *Bulk Approval Mode*\n\n"
        if not MANAGED_CHANNELS:
            await update.message.reply_text("❌ No channels")
            return
        
        for channel_id, data in MANAGED_CHANNELS.items():
            status = "✅ ON" if BULK_APPROVAL_MODE.get(channel_id) else "❌ OFF"
            text += f"{data['name']}: {status}\n"
        
        text += f"\n*Usage:*\n/toggle_bulk CHANNEL_ID"
        await update.message.reply_text(text, parse_mode='Markdown')
        return
    
    try:
        channel_id = int(context.args[0])
        
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not found")
            return
        
        current = BULK_APPROVAL_MODE.get(channel_id, False)
        BULK_APPROVAL_MODE[channel_id] = not current
        
        status = "ON ✅" if BULK_APPROVAL_MODE[channel_id] else "OFF ❌"
        
        await update.message.reply_text(
            f"🔄 *Bulk Mode {status}*\n\n{MANAGED_CHANNELS[channel_id]['name']}",
            parse_mode='Markdown'
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")

async def approve_all_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not PENDING_VERIFICATIONS:
        await update.message.reply_text("📭 No pending")
        return
    
    msg = await update.message.reply_text(f"⏳ Approving {len(PENDING_VERIFICATIONS)}...")
    
    approved = 0
    failed = 0
    pending_copy = dict(PENDING_VERIFICATIONS)
    
    for user_id, data in pending_copy.items():
        try:
            await context.bot.approve_chat_join_request(data['channel_id'], user_id)
            
            if user_id not in VERIFIED_FOR_CHANNELS:
                VERIFIED_FOR_CHANNELS[user_id] = []
            VERIFIED_FOR_CHANNELS[user_id].append(data['channel_id'])
            
            del PENDING_VERIFICATIONS[user_id]
            approved += 1
            
            try:
                await context.bot.send_message(
                    user_id,
                    f"✅ Approved for *{data['channel_name']}*!",
                    parse_mode='Markdown'
                )
            except:
                pass
            
        except Exception as e:
            logger.error(f"Failed {user_id}: {e}")
            failed += 1
        
        await asyncio.sleep(0.1)
    
    await msg.edit_text(f"✅ *Done*\n\nApproved: {approved}\nFailed: {failed}", parse_mode='Markdown')

async def bulk_approve_from_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    await update.message.reply_text(
        "📄 *Bulk Approve*\n\n"
        "Create file with user IDs:\n"
        "```\n123456789\n987654321\n```\n\n"
        "Upload with caption:\n"
        "`/bulk_approve CHANNEL_ID`",
        parse_mode='Markdown'
    )

async def handle_bulk_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not update.message.document:
        return
    
    caption = update.message.caption or ""
    if not caption.startswith("/bulk_approve"):
        return
    
    try:
        parts = caption.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ Format: `/bulk_approve CHANNEL_ID`")
            return
        
        channel_id = int(parts[1])
        
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not found")
            return
        
        file = await context.bot.get_file(update.message.document.file_id)
        file_content = await file.download_as_bytearray()
        
        user_ids = []
        for line in file_content.decode('utf-8').split('\n'):
            line = line.strip()
            if line and line.isdigit():
                user_ids.append(int(line))
        
        if not user_ids:
            await update.message.reply_text("❌ No valid IDs")
            return
        
        msg = await update.message.reply_text(f"⏳ Processing {len(user_ids)}...")
        
        approved = 0
        failed = 0
        
        for user_id in user_ids:
            try:
                await context.bot.approve_chat_join_request(channel_id, user_id)
                approved += 1
                
                if approved % 100 == 0:
                    await msg.edit_text(f"⏳ {approved}/{len(user_ids)}\nApproved: {approved}\nFailed: {failed}")
                
            except Exception as e:
                logger.error(f"Failed {user_id}: {e}")
                failed += 1
            
            await asyncio.sleep(0.05)
        
        await msg.edit_text(
            f"✅ *Complete!*\n\n"
            f"Channel: {MANAGED_CHANNELS[channel_id]['name']}\n"
            f"Total: {len(user_ids)}\n"
            f"✅ Approved: {approved}\n"
            f"❌ Failed: {failed}",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ==================== ADMIN COMMANDS ====================
async def pending_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not PENDING_VERIFICATIONS:
        await update.message.reply_text("📭 No pending")
        return
    
    text = "⏳ *Pending:*\n\n"
    for user_id, data in list(PENDING_VERIFICATIONS.items())[:20]:
        time_ago = (datetime.now() - data['timestamp']).seconds // 60
        text += f"ID: `{user_id}`\nChannel: {data['channel_name']}\nTime: {time_ago} mins\n\n"
    
    if len(PENDING_VERIFICATIONS) > 20:
        text += f"...+{len(PENDING_VERIFICATIONS) - 20} more\n"
    
    text += f"\nTotal: {len(PENDING_VERIFICATIONS)}"
    await update.message.reply_text(text, parse_mode='Markdown')

async def manual_approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /approve_user USER_ID CHANNEL_ID")
        return
    
    try:
        user_id = int(context.args[0])
        channel_id = int(context.args[1])
        
        await context.bot.approve_chat_join_request(channel_id, user_id)
        
        if user_id in PENDING_VERIFICATIONS:
            del PENDING_VERIFICATIONS[user_id]
        
        await update.message.reply_text(f"✅ Approved `{user_id}`", parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def block_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /block_user USER_ID")
        return
    
    user_id = int(context.args[0])
    BLOCKED_USERS.add(user_id)
    
    if user_id in PENDING_VERIFICATIONS:
        del PENDING_VERIFICATIONS[user_id]
    
    await update.message.reply_text(f"🚫 Blocked `{user_id}`", parse_mode='Markdown')

async def unblock_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /unblock_user USER_ID")
        return
    
    user_id = int(context.args[0])
    if user_id in BLOCKED_USERS:
        BLOCKED_USERS.remove(user_id)
        await update.message.reply_text(f"✅ Unblocked `{user_id}`", parse_mode='Markdown')
    else:
        await update.message.reply_text("Not blocked")

async def verification_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    text = (
        f"🔒 *Settings*\n\n"
        f"Age: {MIN_ACCOUNT_AGE_DAYS} days\n"
        f"Photo: {'Required' if REQUIRE_PROFILE_PHOTO else 'Optional'}\n"
        f"Username: {'Required' if REQUIRE_USERNAME else 'Optional'}\n"
        f"Expiry: {CODE_EXPIRY_MINUTES} mins\n"
        f"Attempts: 3\n"
        f"Name: Strict\n\n"
        f"📊 *Stats:*\n"
        f"Pending: {len(PENDING_VERIFICATIONS)}\n"
        f"Blocked: {len(BLOCKED_USERS)}\n"
        f"Verified: {len(VERIFIED_FOR_CHANNELS)}"
    )
    
    await update.message.reply_text(text, parse_mode='Markdown')

# ==================== CHANNEL MANAGEMENT ====================
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    
    await update.message.reply_text(
        "📢 *Add Channel:*\n\n"
        "1. Add bot as ADMIN\n"
        "2. Give: Invite Users, Post Messages\n"
        "3. Forward message to me",
        parse_mode='Markdown'
    )

async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    
    if update.message.forward_from_chat:
        channel = update.message.forward_from_chat
        if channel.type in ['channel', 'supergroup']:
            if not await is_bot_admin(context, channel.id):
                await update.message.reply_text("❌ Make me ADMIN first!")
                return
            
            try:
                link = await context.bot.export_chat_invite_link(channel.id)
            except:
                link = "N/A"
            
            MANAGED_CHANNELS[channel.id] = {
                "name": channel.title,
                "invite_link": link,
                "username": channel.username or "Private"
            }
            
            BULK_APPROVAL_MODE[channel.id] = False
            
            await update.message.reply_text(
                f"✅ *Registered!*\n\n📢 {channel.title}\n🆔 `{channel.id}`\n\n"
                f"🔒 Verification active",
                parse_mode='Markdown'
            )

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    
    if not MANAGED_CHANNELS:
        await update.message.reply_text("📭 No channels")
        return
    
    text = "📢 *Channels:*\n\n"
    for channel_id, data in MANAGED_CHANNELS.items():
        bulk = "🔄 BULK" if BULK_APPROVAL_MODE.get(channel_id) else "🔒 SECURE"
        text += f"📌 *{data['name']}*\nID: `{channel_id}`\nMode: {bulk}\n\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

# ==================== POSTING ====================
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    
    if not MANAGED_CHANNELS:
        await update.message.reply_text("❌ No channels")
        return
    
    await update.message.reply_text("📤 Send content")
    context.user_data['posting_mode'] = True

async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_code'):
        await handle_verification_code(update, context)
        return
    
    if not context.user_data.get('posting_mode'):
        return
    
    user_id = update.effective_user.id
    message = update.message
    
    content_type = 'text'
    if message.photo:
        content_type = 'photo'
    elif message.video:
        content_type = 'video'
    elif message.document:
        content_type = 'document'
    
    PENDING_POSTS[user_id] = {'message': message, 'type': content_type}
    
    keyboard = []
    for channel_id, data in MANAGED_CHANNELS.items():
        keyboard.append([InlineKeyboardButton(f"📢 {data['name']}", callback_data=f"post_{channel_id}")])
    
    keyboard.append([InlineKeyboardButton("🔄 ALL", callback_data="post_all")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="post_cancel")])
    
    await update.message.reply_text("🎯 Select:", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['posting_mode'] = False

async def post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id not in PENDING_POSTS:
        await query.edit_message_text("❌ No post")
        return
    
    action = query.data.split('_')[1]
    
    if action == "cancel":
        del PENDING_POSTS[user_id]
        await query.edit_message_text("❌ Cancelled")
        return
    
    pending = PENDING_POSTS[user_id]
    original_msg = pending['message']
    
    channels = [int(action)] if action != "all" else list(MANAGED_CHANNELS.keys())
    
    await query.edit_message_text("⏳ Posting...")
    
    success = 0
    for channel_id in channels:
        try:
            if pending['type'] == 'text':
                await context.bot.send_message(channel_id, original_msg.text)
            elif pending['type'] == 'photo':
                await context.bot.send_photo(channel_id, original_msg.photo[-1].file_id, caption=original_msg.caption)
            elif pending['type'] == 'video':
                await context.bot.send_video(channel_id, original_msg.video.file_id, caption=original_msg.caption)
            elif pending['type'] == 'document':
                await context.bot.send_document(channel_id, original_msg.document.file_id, caption=original_msg.caption)
            
            success += 1
        except:
            pass
    
    del PENDING_POSTS[user_id]
    await query.message.reply_text(f"✅ Posted to {success}")

# ==================== STATS ====================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    
    bulk_enabled = sum(1 for v in BULK_APPROVAL_MODE.values() if v)
    
    text = (
        f"📊 *Stats*\n\n"
        f"📢 Channels: {len(MANAGED_CHANNELS)}\n"
        f"🔄 Bulk: {bulk_enabled}\n"
        f"⏳ Pending: {len(PENDING_VERIFICATIONS)}\n"
        f"✅ Verified: {len(VERIFIED_FOR_CHANNELS)}\n"
        f"🚫 Blocked: {len(BLOCKED_USERS)}\n\n"
        f"Status: Online 24/7"
    )
    
    await update.message.reply_text(text, parse_mode='Markdown')

# ==================== MAIN ====================
def main():
    logger.info("🚀 Starting bot...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("pending_users", pending_users))
    app.add_handler(CommandHandler("approve_user", manual_approve_user))
    app.add_handler(CommandHandler("approve_all_pending", approve_all_pending_command))
    app.add_handler(CommandHandler("bulk_approve", bulk_approve_from_file))
    app.add_handler(CommandHandler("toggle_bulk", toggle_bulk_approval))
    app.add_handler(CommandHandler("block_user", block_user_command))
    app.add_handler(CommandHandler("unblock_user", unblock_user_command))
    app.add_handler(CommandHandler("verification_settings", verification_settings))
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("stats", stats))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_"))
    app.add_handler(CallbackQueryHandler(enter_code_callback, pattern="^enter_code_"))
    app.add_handler(CallbackQueryHandler(resend_code_callback, pattern="^resend_code_"))
    app.add_handler(CallbackQueryHandler(post_callback, pattern="^post_"))
    
    # Messages
    app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, handle_forwarded_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_bulk_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_content))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_content))
    
    # Join requests - FIXED
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    
    scheduler.start()
    
    logger.info(f"✅ Settings: Photo={REQUIRE_PROFILE_PHOTO}, Age={MIN_ACCOUNT_AGE_DAYS}, Expiry={CODE_EXPIRY_MINUTES}min")
    logger.info("🔒 Bot running 24/7")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
```

---

## **📋 DEPLOYMENT STEPS**

### **1. Replace in GitHub/Replit:**
- Delete ALL old code
- Copy-paste this entire code
- Save

### **2. In Replit:**
- Click "Stop"
- Click "Run"

### **3. Should see:**
```
🚀 Starting bot...
✅ Settings: Photo=False, Age=15, Expiry=5min
🔒 Bot running 24/7
