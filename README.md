# Telegram Channel Manager Bot

A powerful Telegram bot for managing multiple channels with automated approval, verification, and auto-posting features.

## 🚀 Features

- ✅ **Multi-Channel Management**: Manage multiple Telegram channels from one bot
- 🔒 **User Verification**: Manual or bulk approval with verification codes
- 📤 **Auto-Posting**: Schedule automatic posts to channels
- 📊 **User Analytics**: Track user requests and activity
- 🚫 **User Blocking**: Block/unblock users across channels
- 📷 **Image Management**: Upload and manage channel-specific images
- 📈 **Weekly Reports**: Automated weekly statistics

---

## 🌐 Deploy to Railway.app (FREE 24/7 Hosting)

### Step 1: Push to GitHub

1. Create a new repository on GitHub
2. Clone it to your computer:
   ```bash
   git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
   cd YOUR_REPO_NAME
   ```

3. Copy these files into the repository:
   - `super_bot.py`
   - `requirements.txt`
   - `railway.json`
   - `.gitignore`
   - `README.md`

4. Push to GitHub:
   ```bash
   git add .
   git commit -m "Initial commit"
   git push origin main
   ```

### Step 2: Deploy on Railway

1. Go to [railway.app](https://railway.app)
2. Click **"Start a New Project"**
3. Select **"Deploy from GitHub repo"**
4. Choose your repository
5. Railway will automatically detect Python and start deployment

### Step 3: Add Environment Variables

In Railway dashboard:

1. Click on your project
2. Go to **"Variables"** tab
3. Add these variables:
   ```
   BOT_TOKEN=your_telegram_bot_token_here
   ADMIN_ID=your_telegram_user_id_here
   ```

4. **How to get these:**
   - **BOT_TOKEN**: Message [@BotFather](https://t.me/BotFather) on Telegram, create a bot with `/newbot`
   - **ADMIN_ID**: Message [@userinfobot](https://t.me/userinfobot) on Telegram to get your ID

### Step 4: Deploy!

- Railway will automatically deploy your bot
- Check the **"Deployments"** tab for build status
- Once deployed, your bot is live 24/7! ✅

---

## 📝 Bot Commands (Admin Only)

### Channel Management
- `/addchannel` - Add a new channel (forward a message from the channel)
- `/channels` - List all managed channels
- `/toggle_bulk <channel_id>` - Toggle bulk approval mode

### User Management
- `/pending_users` - View pending join requests
- `/approve_user <user_id> <channel_id>` - Manually approve a user
- `/approve_all_pending` - Approve all pending users
- `/block_user <user_id>` - Block a user
- `/unblock_user <user_id>` - Unblock a user

### Posting
- `/post` - Create a new post for channels
- `/upload_images` - Upload images for auto-posting
- `/done_uploading` - Finish uploading images
- `/upload_for_channel <channel_id>` - Upload channel-specific images

### Auto-Posting
- `/enable_autopost <channel_id>` - Enable auto-posting (every 15 min)
- `/disable_autopost <channel_id>` - Disable auto-posting
- `/autopost_status` - Check auto-posting status

### Captions
- `/set_default_caption <text>` - Set default caption for posts
- `/clear_default_caption` - Clear default caption
- `/set_channel_caption <channel_id> <text>` - Set channel-specific caption
- `/clear_channel_caption <channel_id>` - Clear channel caption

### Statistics & Reports
- `/stats` - View bot statistics
- `/user_stats` - View user statistics per channel
- `/export_users` - Export user database as CSV
- `/view_unauthorized` - View unauthorized access attempts
- `/clear_unauthorized` - Clear unauthorized access log

---

## 🔧 Data Persistence

All data (channels, users, images, settings) is stored in `bot_data.json` on Railway's persistent storage.

**Important**: Railway provides persistent storage, so your data won't be lost on restarts!

---

## 🛡️ Security Features

- **Admin-only access**: Only the ADMIN_ID can control the bot
- **Unauthorized access logging**: Tracks all unauthorized command attempts
- **User legitimacy checks**: Verifies user accounts before approval
- **Blocked users list**: Permanently block suspicious users

---

## 📊 Data Saved

The bot automatically saves:
- Managed channels
- User database
- Uploaded images
- Channel-specific settings
- Approval modes
- Auto-post configurations

---

## 🔄 Updates & Maintenance

To update your bot on Railway:

1. Make changes to your code
2. Push to GitHub:
   ```bash
   git add .
   git commit -m "Update description"
   git push
   ```
3. Railway automatically redeploys!

---

## ⚠️ Troubleshooting

### Bot not responding?
- Check Railway logs in the dashboard
- Verify BOT_TOKEN and ADMIN_ID are correct
- Make sure bot is admin in all channels

### Data not saving?
- Check Railway logs for errors
- Verify write permissions

### Auto-posting not working?
- Ensure images are uploaded for the channel
- Check `/autopost_status`
- Verify scheduler is running (check logs)

---

## 🆘 Support

If you need help:
1. Check Railway logs for errors
2. Review this README
3. Ensure all environment variables are set correctly

---

## 📄 License

MIT License - Free to use and modify

---

## ✅ What's Fixed in This Version

1. **Critical Bug Fix**: Fixed `AttributeError: 'NoneType' object has no attribute 'id'` error
2. **Error Handler**: Added global error handler to prevent bot crashes
3. **Railway Optimized**: Configured for 24/7 uptime on Railway.app
4. **Auto-restart**: Bot automatically restarts on failures
5. **Persistent Storage**: All data saved to JSON file

---

**Built for 24/7 operation on Railway.app** 🚀
