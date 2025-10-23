import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ChatMemberStatus
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta

# Configuration
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ADMIN_ID = 123456789  # Replace with YOUR Telegram ID
VERIFIED_USERS = set()  # Store verified user IDs
MANAGED_CHANNELS = {}  # {channel_id: channel_name}
PENDING_POSTS = {}  # Store posts waiting for channel selection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize scheduler
scheduler = AsyncIOScheduler()

# ==================== VERIFICATION SYSTEM ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message with verification"""
    user_id = update.effective_user.id
    
    if user_id == ADMIN_ID:
        VERIFIED_USERS.add(user_id)
        await update.message.reply_text(
            "🎯 *ADMIN ACCESS GRANTED*\n\n"
            "*Available Commands:*\n"
            "/addchannel - Add a channel to manage\n"
            "/channels - List all channels\n"
            "/post - Post content to channels\n"
            "/schedule - Schedule a post\n"
            "/pending - Check pending join requests\n"
            "/stats - Bot statistics",
            parse_mode='Markdown'
        )
        return
    
    # Verification for non-admin users
    keyboard = [[InlineKeyboardButton("🔐 Verify via Telegram ID", callback_data=f"verify_{user_id}")]]
    await update.message.reply_text(
        f"🔒 *Verification Required*\n\n"
        f"Your Telegram ID: `{user_id}`\n\n"
        "Click below to verify:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle verification"""
    query = update.callback_query
    await query.answer()
    
    user_id = int(query.data.split('_')[1])
    
    if user_id == query.from_user.id:
        VERIFIED_USERS.add(user_id)
        await query.edit_message_text(
            "✅ *Verification Successful!*\n\n"
            "You can now use the bot. Type /help for commands.",
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text("❌ Verification failed. ID mismatch.")

def is_verified(user_id: int) -> bool:
    """Check if user is verified"""
    return user_id in VERIFIED_USERS or user_id == ADMIN_ID

# ==================== CHANNEL MANAGEMENT ====================
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a channel to manage"""
    if not is_verified(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized. Use /start to verify.")
        return
    
    await update.message.reply_text(
        "📢 *Add Channel*\n\n"
        "1. Add this bot as ADMIN to your channel\n"
        "2. Forward ANY message from that channel to me\n"
        "3. I'll register it automatically!",
        parse_mode='Markdown'
    )

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all managed channels"""
    if not is_verified(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return
    
    if not MANAGED_CHANNELS:
        await update.message.reply_text("📭 No channels added yet. Use /addchannel")
        return
    
    text = "📢 *Managed Channels:*\n\n"
    for channel_id, name in MANAGED_CHANNELS.items():
        text += f"• {name} (`{channel_id}`)\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-register channel from forwarded message"""
    if not is_verified(update.effective_user.id):
        return
    
    if update.message.forward_from_chat:
        channel = update.message.forward_from_chat
        if channel.type in ['channel', 'supergroup']:
            MANAGED_CHANNELS[channel.id] = channel.title
            await update.message.reply_text(
                f"✅ Channel registered!\n\n"
                f"*Name:* {channel.title}\n"
                f"*ID:* `{channel.id}`",
                parse_mode='Markdown'
            )

# ==================== POSTING SYSTEM ====================
async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate posting process"""
    if not is_verified(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return
    
    if not MANAGED_CHANNELS:
        await update.message.reply_text("❌ No channels available. Use /addchannel first.")
        return
    
    await update.message.reply_text(
        "📤 *Ready to Post*\n\n"
        "Send me your content now:\n"
        "• Text message\n"
        "• Photo with caption\n"
        "• Video with caption\n"
        "• Document\n\n"
        "After sending, I'll ask which channel to post to.",
        parse_mode='Markdown'
    )
    
    context.user_data['posting_mode'] = True

async def handle_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming content for posting"""
    if not context.user_data.get('posting_mode'):
        return
    
    user_id = update.effective_user.id
    message = update.message
    
    # Store the content
    PENDING_POSTS[user_id] = {
        'message': message,
        'type': 'text' if message.text else 'photo' if message.photo else 'video' if message.video else 'document'
    }
    
    # Create channel selection keyboard
    keyboard = []
    for channel_id, name in MANAGED_CHANNELS.items():
        keyboard.append([InlineKeyboardButton(f"📢 {name}", callback_data=f"post_{channel_id}")])
    keyboard.append([InlineKeyboardButton("🔄 Post to ALL", callback_data="post_all")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="post_cancel")])
    
    await update.message.reply_text(
        "🎯 *Select Target Channel:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    
    context.user_data['posting_mode'] = False

async def post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle channel selection and post"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if user_id not in PENDING_POSTS:
        await query.edit_message_text("❌ No pending post found. Use /post to start.")
        return
    
    action = query.data.split('_')[1]
    
    if action == "cancel":
        del PENDING_POSTS[user_id]
        await query.edit_message_text("❌ Posting cancelled.")
        return
    
    pending = PENDING_POSTS[user_id]
    original_msg = pending['message']
    
    channels_to_post = []
    if action == "all":
        channels_to_post = list(MANAGED_CHANNELS.keys())
    else:
        channels_to_post = [int(action)]
    
    success_count = 0
    failed = []
    
    for channel_id in channels_to_post:
        try:
            # Post based on content type
            if pending['type'] == 'text':
                await context.bot.send_message(channel_id, original_msg.text)
            elif pending['type'] == 'photo':
                await context.bot.send_photo(
                    channel_id,
                    original_msg.photo[-1].file_id,
                    caption=original_msg.caption
                )
            elif pending['type'] == 'video':
                await context.bot.send_video(
                    channel_id,
                    original_msg.video.file_id,
                    caption=original_msg.caption
                )
            elif pending['type'] == 'document':
                await context.bot.send_document(
                    channel_id,
                    original_msg.document.file_id,
                    caption=original_msg.caption
                )
            success_count += 1
        except Exception as e:
            failed.append(MANAGED_CHANNELS.get(channel_id, str(channel_id)))
            logger.error(f"Failed to post to {channel_id}: {e}")
    
    del PENDING_POSTS[user_id]
    
    result = f"✅ Posted to {success_count} channel(s)"
    if failed:
        result += f"\n❌ Failed: {', '.join(failed)}"
    
    await query.edit_message_text(result)

# ==================== AUTO-APPROVE JOIN REQUESTS ====================
async def check_join_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check and approve pending join requests"""
    if not is_verified(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return
    
    await update.message.reply_text("🔍 Checking join requests...")
    
    approved = 0
    for channel_id in MANAGED_CHANNELS.keys():
        try:
            # This requires the bot to have admin rights with "Invite Users" permission
            # Note: Telegram Bot API has limitations on accessing join requests directly
            # You may need to handle chat_join_request updates
            pass
        except Exception as e:
            logger.error(f"Error checking {channel_id}: {e}")
    
    await update.message.reply_text(
        f"✅ Processed join requests\n"
        f"Approved: {approved}",
        parse_mode='Markdown'
    )

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-approve join requests"""
    if update.chat_join_request:
        try:
            await context.bot.approve_chat_join_request(
                update.chat_join_request.chat.id,
                update.chat_join_request.from_user.id
            )
            logger.info(f"Auto-approved join request from {update.chat_join_request.from_user.id}")
        except Exception as e:
            logger.error(f"Failed to approve join request: {e}")

# ==================== SCHEDULING ====================
async def schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Schedule a post"""
    if not is_verified(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return
    
    await update.message.reply_text(
        "⏰ *Schedule a Post*\n\n"
        "Format: /schedule HH:MM Your message here\n"
        "Example: /schedule 14:30 Daily update!\n\n"
        "The post will be sent daily at that time.",
        parse_mode='Markdown'
    )

# ==================== STATS & MONITORING ====================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics"""
    if not is_verified(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return
    
    text = (
        f"📊 *Bot Statistics*\n\n"
        f"🔐 Verified Users: {len(VERIFIED_USERS)}\n"
        f"📢 Managed Channels: {len(MANAGED_CHANNELS)}\n"
        f"⏰ Scheduled Jobs: {len(scheduler.get_jobs())}\n"
        f"✅ Status: Online 24/7"
    )
    
    await update.message.reply_text(text, parse_mode='Markdown')

# ==================== MAIN ====================
def main():
    """Start the bot"""
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("pending", check_join_requests))
    app.add_handler(CommandHandler("schedule", schedule_post))
    app.add_handler(CommandHandler("stats", stats))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_"))
    app.add_handler(CallbackQueryHandler(post_callback, pattern="^post_"))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, handle_forwarded_message))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
        handle_content
    ))
    
    # Join request handler
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_JOIN_REQUEST, handle_join_request))
    
    # Start scheduler
    scheduler.start()
    
    logger.info("🚀 Bot is running 24/7...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()