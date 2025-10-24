import os
import logging
from datetime import datetime
import random
import string
import re
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ChatJoinRequestHandler, ContextTypes, filters
from telegram.constants import ChatMemberStatus
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from PIL import Image
from io import BytesIO

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

# Image storage
UPLOADED_IMAGES = []  # [{file_id, caption, filename}]
CHANNEL_SPECIFIC_IMAGES = {}  # {channel_id: [{file_id, caption}]}
CURRENT_IMAGE_INDEX = {}  # {channel_id: index}
AUTO_POST_ENABLED = {}  # {channel_id: True/False}
POSTING_INTERVAL_HOURS = 1

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

async def auto_post_job(context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    if not AUTO_POST_ENABLED.get(channel_id):
        return
    
    # Check channel-specific images first
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
    
    try:
        await context.bot.send_photo(
            chat_id=channel_id,
            photo=image['file_id'],
            caption=image.get('caption', '')
        )
        logger.info(f"Posted to {channel_id}")
        
        # Loop back to start
        CURRENT_IMAGE_INDEX[channel_id] = (idx + 1) % len(images)
    except Exception as e:
        logger.error(f"Post failed: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id == ADMIN_ID:
        VERIFIED_USERS.add(user_id)
        await update.message.reply_text(
            "🎯 *ADMIN PANEL*\n\n"
            "*Channels:* /addchannel /channels\n"
            "*Users:* /approve\\_all\\_pending /block\\_user ID\n"
            "*Images:* /upload\\_images /list\\_images /clear\\_images\n"
            "*Auto-Post:* /enable\\_autopost CHANNEL\\_ID /disable\\_autopost CHANNEL\\_ID\n"
            "*Channel Images:* /upload\\_for\\_channel CHANNEL\\_ID\n"
            "*Status:* /autopost\\_status /stats",
            parse_mode='Markdown'
        )
        return
    
    keyboard = [[InlineKeyboardButton("🔐 Verify", callback_data=f"verify_{user_id}")]]
    await update.message.reply_text(
        f"🔒 Verification Required\n\nID: `{user_id}`",
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
    await query.edit_message_text("✅ Verified!")

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_join_request:
        return
    
    user_id = update.chat_join_request.from_user.id
    channel_id = update.chat_join_request.chat.id
    channel_name = MANAGED_CHANNELS.get(channel_id, {}).get("name", "Unknown")
    
    if BULK_APPROVAL_MODE.get(channel_id, False):
        try:
            await context.bot.approve_chat_join_request(channel_id, user_id)
            return
        except:
            return
    
    if user_id in BLOCKED_USERS:
        try:
            await context.bot.decline_chat_join_request(channel_id, user_id)
        except:
            pass
        return
    
    legitimacy_check = await check_user_legitimacy(context, user_id)
    if not legitimacy_check["legitimate"]:
        try:
            await context.bot.decline_chat_join_request(channel_id, user_id)
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
            [InlineKeyboardButton("✅ Enter", callback_data=f"enter_code_{user_id}")],
            [InlineKeyboardButton("🔄 Resend", callback_data=f"resend_code_{user_id}")]
        ]
        await context.bot.send_message(
            user_id,
            f"🔐 Code: `{verification_code}`\n\n⏱️ {CODE_EXPIRY_MINUTES} mins",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    except:
        pass

async def enter_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📝 Reply with code")
    context.user_data['awaiting_code'] = True

async def resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in PENDING_VERIFICATIONS:
        return
    verification = PENDING_VERIFICATIONS[user_id]
    new_code = generate_verification_code()
    verification['code'] = new_code
    verification['timestamp'] = datetime.now()
    await query.edit_message_text(f"🔐 New Code: `{new_code}`", parse_mode='Markdown')

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
        await update.message.reply_text("❌ Expired")
        return
    
    verification['attempts'] += 1
    
    if submitted_code == verification['code']:
        context.user_data['awaiting_code'] = False
        try:
            await context.bot.approve_chat_join_request(verification['channel_id'], user_id)
            await update.message.reply_text(f"✅ Approved!")
            if user_id not in VERIFIED_FOR_CHANNELS:
                VERIFIED_FOR_CHANNELS[user_id] = []
            VERIFIED_FOR_CHANNELS[user_id].append(verification['channel_id'])
            del PENDING_VERIFICATIONS[user_id]
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
    else:
        remaining = verification['max_attempts'] - verification['attempts']
        if remaining > 0:
            await update.message.reply_text(f"❌ Wrong. {remaining} left")
        else:
            del PENDING_VERIFICATIONS[user_id]
            context.user_data['awaiting_code'] = False
            BLOCKED_USERS.add(user_id)
            await update.message.reply_text("❌ Blocked")

# IMAGE UPLOAD HANDLERS
async def upload_images_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "📤 *Upload Images*\n\n"
        "Send photos one by one or as album\n"
        "Add caption for each image (optional)\n\n"
        "Images will be posted to ALL channels unless you use /upload\\_for\\_channel",
        parse_mode='Markdown'
    )
    context.user_data['uploading_mode'] = 'general'

async def upload_for_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        text = "📢 *Upload for Specific Channel*\n\n"
        for cid, data in MANAGED_CHANNELS.items():
            text += f"{data['name']}: `{cid}`\n"
        text += "\nUsage: `/upload_for_channel CHANNEL_ID`"
        await update.message.reply_text(text, parse_mode='Markdown')
        return
    
    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Invalid channel")
            return
        
        context.user_data['uploading_mode'] = 'channel_specific'
        context.user_data['upload_channel_id'] = channel_id
        await update.message.reply_text(f"📤 Send images for {MANAGED_CHANNELS[channel_id]['name']}")
    except:
        await update.message.reply_text("❌ Invalid ID")

async def handle_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not update.message.photo:
        return
    
    uploading_mode = context.user_data.get('uploading_mode')
    if not uploading_mode:
        return
    
    photo = update.message.photo[-1]
    caption = update.message.caption or ""
    
    image_data = {
        'file_id': photo.file_id,
        'caption': caption,
        'filename': f"image_{len(UPLOADED_IMAGES)}.jpg"
    }
    
    if uploading_mode == 'general':
        UPLOADED_IMAGES.append(image_data)
        await update.message.reply_text(f"✅ Added! Total: {len(UPLOADED_IMAGES)}")
    elif uploading_mode == 'channel_specific':
        channel_id = context.user_data.get('upload_channel_id')
        if channel_id:
            if channel_id not in CHANNEL_SPECIFIC_IMAGES:
                CHANNEL_SPECIFIC_IMAGES[channel_id] = []
            CHANNEL_SPECIFIC_IMAGES[channel_id].append(image_data)
            await update.message.reply_text(f"✅ Added! Channel total: {len(CHANNEL_SPECIFIC_IMAGES[channel_id])}")

async def list_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    text = f"📂 *General Images:* {len(UPLOADED_IMAGES)}\n\n"
    
    for channel_id, images in CHANNEL_SPECIFIC_IMAGES.items():
        channel_name = MANAGED_CHANNELS.get(channel_id, {}).get('name', 'Unknown')
        text += f"📢 {channel_name}: {len(images)} images\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def clear_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global UPLOADED_IMAGES
    if update.effective_user.id != ADMIN_ID:
        return
    UPLOADED_IMAGES = []
    await update.message.reply_text("🗑️ Cleared general images")

# AUTO-POSTING
async def enable_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /enable\\_autopost CHANNEL\\_ID")
        return
    
    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Channel not found")
            return
        
        # Check if images available
        has_images = UPLOADED_IMAGES or (channel_id in CHANNEL_SPECIFIC_IMAGES and CHANNEL_SPECIFIC_IMAGES[channel_id])
        if not has_images:
            await update.message.reply_text("❌ Upload images first")
            return
        
        AUTO_POST_ENABLED[channel_id] = True
        
        # Schedule job
        scheduler.add_job(
            auto_post_job,
            trigger=CronTrigger(hour='*'),
            args=[context, channel_id],
            id=f'autopost_{channel_id}',
            replace_existing=True
        )
        
        await update.message.reply_text(
            f"🚀 *Auto-Post Enabled!*\n\n"
            f"Channel: {MANAGED_CHANNELS[channel_id]['name']}\n"
            f"Interval: Every 1 hour\n\n"
            f"Disable: /disable\\_autopost {channel_id}",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def disable_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /disable\\_autopost CHANNEL\\_ID")
        return
    
    try:
        channel_id = int(context.args[0])
        AUTO_POST_ENABLED[channel_id] = False
        try:
            scheduler.remove_job(f'autopost_{channel_id}')
        except:
            pass
        await update.message.reply_text("⏹️ Stopped")
    except:
        await update.message.reply_text("❌ Invalid ID")

async def autopost_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    text = "🤖 *Auto-Post Status*\n\n"
    
    for channel_id, data in MANAGED_CHANNELS.items():
        status = "🟢 ON" if AUTO_POST_ENABLED.get(channel_id) else "🔴 OFF"
        
        if channel_id in CHANNEL_SPECIFIC_IMAGES:
            image_count = len(CHANNEL_SPECIFIC_IMAGES[channel_id])
        else:
            image_count = len(UPLOADED_IMAGES)
        
        current_idx = CURRENT_IMAGE_INDEX.get(channel_id, 0)
        
        text += f"*{data['name']}*\n{status} | {image_count} images | Next: #{current_idx+1}\n\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

# BULK APPROVAL
async def toggle_bulk_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        text = "🔄 *Bulk Mode*\n\n"
        for cid, data in MANAGED_CHANNELS.items():
            status = "ON" if BULK_APPROVAL_MODE.get(cid) else "OFF"
            text += f"{data['name']}: {status}\n"
        text += "\nUsage: `/toggle_bulk CHANNEL_ID`"
        await update.message.reply_text(text, parse_mode='Markdown')
        return
    
    try:
        channel_id = int(context.args[0])
        if channel_id not in MANAGED_CHANNELS:
            await update.message.reply_text("❌ Not found")
            return
        current = BULK_APPROVAL_MODE.get(channel_id, False)
        BULK_APPROVAL_MODE[channel_id] = not current
        status = "ON" if BULK_APPROVAL_MODE[channel_id] else "OFF"
        await update.message.reply_text(f"🔄 Bulk Mode {status}")
    except:
        await update.message.reply_text("❌ Invalid ID")

async def approve_all_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not PENDING_VERIFICATIONS:
        await update.message.reply_text("📭 No pending")
        return
    msg = await update.message.reply_text(f"⏳ Approving {len(PENDING_VERIFICATIONS)}...")
    approved = 0
    for user_id, data in dict(PENDING_VERIFICATIONS).items():
        try:
            await context.bot.approve_chat_join_request(data['channel_id'], user_id)
            del PENDING_VERIFICATIONS[user_id]
            approved += 1
        except:
            pass
        await asyncio.sleep(0.1)
    await msg.edit_text(f"✅ Approved: {approved}")

# CHANNEL MANAGEMENT
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    await update.message.reply_text("📢 Add bot as ADMIN, then forward a message from channel to me")

async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    if update.message.forward_from_chat:
        channel = update.message.forward_from_chat
        if channel.type in ['channel', 'supergroup']:
            if not await is_bot_admin(context, channel.id):
                await update.message.reply_text("❌ Make me ADMIN first!")
                return
            MANAGED_CHANNELS[channel.id] = {"name": channel.title}
            BULK_APPROVAL_MODE[channel.id] = False
            await update.message.reply_text(f"✅ Registered!\n\n{channel.title}\n`{channel.id}`", parse_mode='Markdown')

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    if not MANAGED_CHANNELS:
        await update.message.reply_text("📭 No channels")
        return
    text = "📢 *Channels:*\n\n"
    for cid, data in MANAGED_CHANNELS.items():
        text += f"*{data['name']}*\n`{cid}`\n\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args:
        return
    user_id = int(context.args[0])
    BLOCKED_USERS.add(user_id)
    await update.message.reply_text(f"🚫 Blocked `{user_id}`", parse_mode='Markdown')

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_verified(update.effective_user.id):
        return
    active_autoposts = sum(1 for v in AUTO_POST_ENABLED.values() if v)
    text = (
        f"📊 *Stats*\n\n"
        f"Channels: {len(MANAGED_CHANNELS)}\n"
        f"Images: {len(UPLOADED_IMAGES)}\n"
        f"Auto-Posts: {active_autoposts} active\n"
        f"Pending: {len(PENDING_VERIFICATIONS)}\n"
        f"Blocked: {len(BLOCKED_USERS)}"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

def main():
    logger.info("🚀 Starting...")
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("toggle_bulk", toggle_bulk_approval))
    app.add_handler(CommandHandler("approve_all_pending", approve_all_pending))
    app.add_handler(CommandHandler("block_user", block_user))
    app.add_handler(CommandHandler("upload_images", upload_images_command))
    app.add_handler(CommandHandler("upload_for_channel", upload_for_channel_command))
    app.add_handler(CommandHandler("list_images", list_images))
    app.add_handler(CommandHandler("clear_images", clear_images))
    app.add_handler(CommandHandler("enable_autopost", enable_autopost))
    app.add_handler(CommandHandler("disable_autopost", disable_autopost))
    app.add_handler(CommandHandler("autopost_status", autopost_status))
    app.add_handler(CommandHandler("stats", stats))
    
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_"))
    app.add_handler(CallbackQueryHandler(enter_code_callback, pattern="^enter_code_"))
    app.add_handler(CallbackQueryHandler(resend_code_callback, pattern="^resend_code_"))
    
    app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, handle_forwarded_message))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_image_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_verification_code))
    
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    
    scheduler.start()
    logger.info("✅ Running 24/7")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
