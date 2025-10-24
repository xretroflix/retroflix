import os
import logging
from datetime import datetime, timedelta
import random
import string
import re
import asyncio
import requests
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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
from mega import Mega

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

# Auto-posting settings
MEGA_FOLDER_URL = None
MEGA_IMAGES = []
CURRENT_IMAGE_INDEX = 0
AUTO_POST_ENABLED = False
AUTO_POST_CHANNELS = []
POST_CAPTION_TEMPLATE = ""
POSTING_INTERVAL_HOURS = 1

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
    if not name or len(name) < 2:
        return True
    
    letters_and_numbers = re.sub(r'[^a-zA-Z0-9]', '', name)
    
    if len(letters_and_numbers) < 2:
        return True
    
    spam_patterns = [
        r'^[0-9]+$',
        r'^[_\-\.]+$',
        r'^\s+$',
    ]
    
    for pattern in spam_patterns:
        if re.match(pattern, name):
            return True
    
    total_chars = len(name)
    special_chars = len(re.findall(r'[^a-zA-Z0-9\s]', name))
    
    if special_chars / total_chars > 0.7:
        return True
    
    return False

async def check_user_legitimacy(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
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

# ==================== MEGA.NZ FUNCTIONS ====================
def load_mega_images(folder_url: str) -> list:
    """Load all image URLs from Mega.nz public folder"""
    try:
        mega = Mega()
        m = mega.login()
        
        logger.info("Connecting to Mega.nz...")
        
        # Get folder contents
        files = m.get_files_in_node(folder_url)
        
        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
        images = []
        
        for file_id, file_data in files.items():
            if file_data['t'] == 0:  # It's a file
                file_name = file_data['a']['n']
                if any(file_name.lower().endswith(ext) for ext in image_extensions):
                    # Get public link
                    link = m.get_link(file_id)
                    images.append({
                        'name': file_name,
                        'url': link,
                        'id': file_id
                    })
        
        logger.info(f"Loaded {len(images)} images from Mega.nz")
        return images
        
    except Exception as e:
        logger.error(f"Error loading Mega images: {e}")
        return []

def download_image_from_mega(image_url: str) -> BytesIO:
    """Download image from Mega.nz URL"""
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code == 200:
            return BytesIO(response.content)
        return None
    except Exception as e:
        logger.error(f"Error downloading image: {e}")
        return None

async def auto_post_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job to post images hourly"""
    global CURRENT_IMAGE_INDEX
    
    if not AUTO_POST_ENABLED or not MEGA_IMAGES or not AUTO_POST_CHANNELS:
        return
    
    try:
        # Get current image
        image = MEGA_IMAGES[CURRENT_IMAGE_INDEX]
        
        logger.info(f"Auto-posting image: {image['name']}")
        
        # Download image
        image_data = download_image_from_mega(image['url'])
        
        if not image_data:
            logger.error("Failed to download image")
            return
        
        # Prepare caption
        caption = POST_CAPTION_TEMPLATE.replace('{filename}', image['name'])
        caption = caption.replace('{time}', datetime.now().strftime('%I:%M %p'))
        caption = caption.replace('{date}', datetime.now().strftime('%B %d, %Y'))
        
        # Post to all enabled channels
        success_count = 0
        for channel_id in AUTO_POST_CHANNELS:
            try:
                await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=image_data,
                    caption=caption
                )
                LAST_POST_TIME[channel_id] = datetime.now()
                success_count += 1
                logger.info(f"Posted to channel {channel_id}")
                
                # Reset BytesIO position for next channel
                image_data.seek(0)
                
            except Exception as e:
                logger.error(f"Failed to post to {channel_id}: {e}")
        
        # Notify admin
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"✅ Auto-posted!\n\n"
                f"Image: {image['name']}\n"
                f"Posted to: {success_count}/{len(AUTO_POST_CHANNELS)} channels\n"
                f"Time: {datetime.now().strftime('%I:%M %p')}"
            )
        except:
            pass
        
        # Move to next image
        CURRENT_IMAGE_INDEX = (CURRENT_IMAGE_INDEX + 1) % len(MEGA_IMAGES)
        
    except Exception as e:
        logger.error(f"Auto-post job failed: {e}")

# ==================== START & VERIFICATION ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id == ADMIN_ID:
        VERIFIED_USERS.add(user_id)
        
        settings_text = (
            f"Age: {MIN_ACCOUNT_AGE_DAYS} days\n"
            f"Photo: {'Required' if REQUIRE_PROFILE_PHOTO else 'Optional'}\n"
            f"Code: {CODE_EXPIRY_MINUTES} mins\n"
            f"Name: Strict"
        )
        
        await update.message.reply_text(
            "🎯 *SUPER-POWERFUL BOT - ADMIN*\n\n"
            "*📢 Channel:*\n"
            "/addchannel - Add channel\n"
            "/channels - List channels\n"
            "/toggle\\_bulk - Bulk approval mode\n\n"
            "*👥 Users:*\n"
            "/pending\\_users - View pending\n"
            "/approve\\_user USER\\_ID CHANNEL\\_ID\n"
            "/approve\\_all\\_pending - Approve all\n"
            "/bulk\\_approve - Upload file\n"
            "/block\\_user USER\\_ID\n"
            "/unblock\\_user USER\\_ID\n"
            "/verification\\_settings\n\n"
            "*📤 Content:*\n"
            "/post - Post content manually\n\n"
            "*🤖 Auto-Posting:*\n"
            "/setup\\_mega - Setup Mega.nz folder\n"
            "/set\\_caption - Set post caption\n"
            "/set\\_interval - Set posting interval\n"
            "/select\\_channels - Choose channels\n"
            "/start\\_autopost - Start auto-posting\n"
            "/stop\\_autopost - Stop auto-posting\n"
            "/autopost\\_status - Check status\n\n"
            "*📊 Stats:*\n"
            "/stats - Statistics\n\n"
            f"🔒 *Settings:*\n{settings_text}",
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
    await query.edit_message_text("✅ *Verified!*", parse_mode='Markdown')

# ==================== JOIN REQUEST HANDLER ====================
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_join_request:
        return
    
    user_id = update.chat_join_request.from_user.id
    channel_id = update.chat_join_request.chat.id
    channel_name = MANAGED_CHANNELS.get(channel_id, {}).get("name", "Unknown")
    user = update.chat_join_request.from_user
    
    logger.info(f"📥 Join request: {user_id} → {channel_name}")
    
    if BULK_APPROVAL_MODE.get(channel_id, False):
        try:
            await context.bot.approve_chat_join_request(channel_id, user_id)
            logger.info(f"✅ Bulk approved {user_id}")
            return
        except Exception as e:
            logger.error(f"Bulk approval failed: {e}")
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
    
    if (datetime.now() - verification['timestamp']).seconds > (CODE_EXPIRY_MINUTES * 60):
        del PENDING_VERIFICATIONS[user_id]
        context.user_data['awaiting_code'] = False
        await update.message.reply_text("❌ *Code Expired*", parse_mode='Markdown')
        try:
            await context.bot.decline_chat_join_request(verification['channel_id'], user_id)
        except:
            pass
        return
    
    verification['attempts'] += 1
    
    if submitted_code == verification['code']:
        context.user_data['awaiting_code'] = False
        
        try:
            await context.bot.approve_chat_join_request(verification['channel_id'], user_id)
            
            await update.message.reply_text(
                f"✅ *Verified!*\n\nWelcome to *{verification['channel_name']}*! 🎉",
                parse_mode='Markdown'
            )
            
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
            except:
                pass

# ==================== AUTO-POSTING COMMANDS ====================
async def setup_mega(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text(
            "📂 *Setup Mega.nz Folder*\n\n"
            "*Steps:*\n"
            "1. Upload images to Mega.nz folder\n"
            "2. Right-click folder → Get Link\n"
            "3. Copy the public link\n"
            "4. Send: /setup\\_mega YOUR\\_LINK\n\n"
            "*Example:*\n"
            "`/setup_mega https://mega.nz/folder/ABC123#xyz`",
            parse_mode='Markdown'
        )
        return
    
    global MEGA_FOLDER_URL, MEGA_IMAGES
    
    folder_url = context.args[0]
    
    msg = await update.message.reply_text("⏳ Loading images from Mega.nz...")
    
    images = load_mega_images(folder_url)
    
    if images:
        MEGA_FOLDER_URL = folder_url
        MEGA_IMAGES = images
        
        await msg.edit_text(
            f"✅ *Mega.nz Connected!*\n\n"
            f"Found: {len(images)} images\n\n"
            f"*Next steps:*\n"
            f"1. /set\\_caption - Set caption template\n"
            f"2. /select\\_channels - Choose channels\n"
            f"3. /start\\_autopost - Begin posting",
            parse_mode='Markdown'
        )
    else:
        await msg.edit_text("❌ Failed to load images. Check your Mega.nz link.")

async def set_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text(
            "📝 *Set Caption Template*\n\n"
            "*Usage:*\n"
            "`/set_caption Your caption here`\n\n"
            "*Variables:*\n"
            "`{filename}` - Image filename\n"
            "`{time}` - Current time\n"
            "`{date}` - Current date\n\n"
            "*Example:*\n"
            "`/set_caption Daily Update 🌟\n\nPosted at {time}`",
            parse_mode='Markdown'
        )
        return
    
    global POST_CAPTION_TEMPLATE
    POST_CAPTION_TEMPLATE = " ".join(context.args)
    
    await update.message.reply_text(
        f"✅ *Caption Set!*\n\n"
        f"Template:\n{POST_CAPTION_TEMPLATE}\n\n"
        f"Next: /select\\_channels",
        parse_mode='Markdown'
    )

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global POSTING_INTERVAL_HOURS
    
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text(
            f"⏱️ *Current Interval:* {POSTING_INTERVAL_HOURS} hour(s)\n\n"
            f"*Change:*\n"
            f"`/set_interval 1` - Every hour\n"
            f"`/set_interval 2` - Every 2 hours\n"
            f"`/set_interval 0.5` - Every 30 mins",
            parse_mode='Markdown'
        )
        return
    
    try:
        interval = float(context.args[0])
        if interval < 0.1:
            await update.message.reply_text("❌ Minimum interval: 0.1 hours (6 mins)")
            return
        
        POSTING_INTERVAL_HOURS = interval
        
        await update.message.reply_text(
            f"✅ *Interval Updated!*\n\n"
            f"Posts every: {interval} hour(s)\n\n"
            f"Restart auto-post for changes to take effect.",
            parse_mode='Markdown'
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid number")

async def select_channels_for_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not MANAGED_CHANNELS:
        await update.message.reply_text("❌ No channels. Use /addchannel first.")
        return
    
    if not context.args:
        text = "📢 *Select Channels for Auto-Posting*\n\n"
        text += "*Your Channels:*\n"
        for channel_id, data in MANAGED_CHANNELS.items():
            selected = "✅" if channel_id in AUTO_POST_CHANNELS else "❌"
            text += f"{selected} {data['name']} - `{channel_id}`\n"
        
        text += "\n*Usage:*\n"
        text += "`/select_channels CHANNEL_ID1 CHANNEL_ID2...`\n"
        text += "Or:\n"
        text += "`/select_channels all` - Select all channels"
        
        await update.message.reply_text(text, parse_mode='Markdown')
        return
    
    global AUTO_POST_CHANNELS
    
    if context.args[0].lower() == 'all':
        AUTO_POST_CHANNELS = list(MANAGED_CHANNELS.keys())
        await update.message.reply_text(
            f"✅ *All Channels Selected!*\n\n"
            f"Auto-posting to: {len(AUTO_POST_CHANNELS)} channels\n\n"
            f"Next: /start\\_autopost",
            parse_mode='Markdown'
        )
        return
    
    selected = []
    for arg in context.args:
        try:
            channel_id = int(arg)
            if channel_id in MANAGED_CHANNELS:
                selected.append(channel_id)
        except ValueError:
            pass
    
    if selected:
        AUTO_POST_CHANNELS = selected
        channel_names = [MANAGED_CHANNELS[cid]['name'] for cid in selected]
        
        await update.message.reply_text(
            f"✅ *Channels Selected!*\n\n"
            f"Auto-posting to:\n" + "\n".join(f"• {name}" for name in channel_names) +
            f"\n\nNext: /start\\_autopost",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ No valid channels found")

async def start_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    global AUTO_POST_ENABLED
    
    if not MEGA_IMAGES:
        await update.message.reply_text("❌ Setup Mega.nz first: /setup\\_mega")
        return
    
    if not AUTO_POST_CHANNELS:
        await update.message.reply_text("❌ Select channels first: /select\\_channels")
        return
    
    if AUTO_POST_ENABLED:
        await update.message.reply_text("⚠️ Auto-posting already running!")
        return
    
    AUTO_POST_ENABLED = True
    
    # Schedule hourly job
    scheduler.add_job(
        auto_post_job,
        trigger=CronTrigger(hour=f'*/{int(POSTING_INTERVAL_HOURS)}'),
        args=[context],
        id='auto_post_job',
        replace_existing=True
    )
    
    await update.message.reply_text(
        f"🚀 *Auto-Posting Started!*\n\n"
        f"📂 Images: {len(MEGA_IMAGES)}\n"
        f"📢 Channels: {len(AUTO_POST_CHANNELS)}\n"
        f"⏱️ Interval: {POSTING_INTERVAL_HOURS} hour(s)\n"
        f"📝 Caption: {POST_CAPTION_TEMPLATE[:50]}...\n\n"
        f"✅ Bot will post automatically!\n\n"
        f"Use /stop\\_autopost to stop",
        parse_mode='Markdown'
    )
    
    logger.info("Auto-posting started")

async def stop_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    global AUTO_POST_ENABLED
    
    if not AUTO_POST_ENABLED:
        await update.message.reply_text("ℹ️ Auto-posting not running")
        return
    
    AUTO_POST_ENABLED = False
    
    try:
        scheduler.remove_job('auto_post_job')
    except:
        pass
    
    await update.message.reply_text("⏹️ *Auto-Posting Stopped*", parse_mode='Markdown')
    logger.info("Auto-posting stopped")

async def autopost_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    status = "🟢 RUNNING" if AUTO_POST_ENABLED else "🔴 STOPPED"
    
    text = f"🤖 *Auto-Post Status*\n\n"
    text += f"Status: {status}\n"
    text += f"Images: {len(MEGA_IMAGES)}\n"
    text += f"Current Index: {CURRENT_IMAGE_INDEX + 1}/{len(MEGA_IMAGES)}\n"
    text += f"Channels: {len(AUTO_POST_CHANNELS)}\n"
    text += f"Interval: {POSTING_INTERVAL_HOURS} hour(s)\n"
    text += f"Caption: {POST_CAPTION_TEMPLATE[:50]}..."
    
    if AUTO_POST_ENABLED and MEGA_IMAGES:
        text += f"\n\n*Next Image:*\n{MEGA_IMAGES[CURRENT_IMAGE_INDEX]['name']}"
    
    await update.message.reply_text(text, parse_mode='Markdown')

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
        
        text += f"\n*Usage:*\n/toggle\\_bulk CHANNEL\\_ID"
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
        await update.message.reply_text("Usage: /approve\\_user USER\\_ID CHANNEL\\_ID")
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
        await update.message.reply_text("Usage: /block\\_user USER\\_ID")
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
        await update.message.reply_text("Usage: /unblock\\_user USER\\_ID")
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
                f"✅ *Registered!*\n\n📢 {channel.title}\n🆔 `{channel.id}`",
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
    autopost_status = "🟢 Running" if AUTO_POST_ENABLED else "🔴 Stopped"
    
    text = (
        f"📊 *Stats*\n\n"
        f"📢 Channels: {len(MANAGED_CHANNELS)}\n"
        f"🔄 Bulk: {bulk_enabled}\n"
        f"⏳ Pending: {len(PENDING_VERIFICATIONS)}\n"
        f"✅ Verified: {len(VERIFIED_FOR_CHANNELS)}\n"
        f"🚫 Blocked: {len(BLOCKED_USERS)}\n"
        f"🤖 Auto-Post: {autopost_status}\n"
        f"📂 Images: {len(MEGA_IMAGES)}\n\n"
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
    app.add_handler(CommandHandler("toggle_bulk", toggle_bulk_approval))
    app.add_handler(CommandHandler("block_user", block_user_command))
    app.add_handler(CommandHandler("unblock_user", unblock_user_command))
    app.add_handler(CommandHandler("verification_settings", verification_settings))
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("stats", stats))
    
    # Auto-posting commands
    app.add_handler(CommandHandler("setup_mega", setup_mega))
    app.add_handler(CommandHandler("set_caption", set_caption))
    app.add_handler(CommandHandler("set_interval", set_interval))
    app.add_handler(CommandHandler("select_channels", select_channels_for_autopost))
    app.add_handler(CommandHandler("start_autopost", start_autopost))
    app.add_handler(CommandHandler("stop_autopost", stop_autopost))
    app.add_handler(CommandHandler("autopost_status", autopost_status))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_"))
    app.add_handler(CallbackQueryHandler(enter_code_callback, pattern="^enter_code_"))
    app.add_handler(CallbackQueryHandler(resend_code_callback, pattern="^resend_code_"))
    app.add_handler(CallbackQueryHandler(post_callback, pattern="^post_"))
    
    # Messages
    app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, handle_forwarded_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_content))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_content))
    
    # Join requests
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    
    scheduler.start()
    
    logger.info(f"✅ Settings: Photo={REQUIRE_PROFILE_PHOTO}, Age={MIN_ACCOUNT_AGE_DAYS}, Expiry={CODE_EXPIRY_MINUTES}min")
    logger.info("🔒 Bot running 24/7")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
