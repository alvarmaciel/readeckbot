"""
A Telegram bot that interfaces with a self-hosted Readeck instance to manage bookmarks.

Features:
- Save bookmarks by sending a URL (with optional title and tags).
- Supports per-user Readeck token configuration via:
    • /token <YOUR_READECK_TOKEN>
    • /register <password>  (your Telegram user ID is used as username)
- Configuration (Telegram token and Readeck URL) is loaded from a .env file.
- Uses a persistent dictionary (JSON file) to store user tokens.
"""

# /// script
# dependencies = [
#   "requests",
#   "python-telegram-bot",
#   "rich",
#   "python-dotenv"
# ]
# ///

import os
import re
import json
import requests
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, MessageHandler, filters, CallbackContext, ApplicationBuilder
from rich.logging import RichHandler
import logging

# Configure rich logging
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)]
)
logger = logging.getLogger(__name__)

class PersistentDict(dict):
    """A simple persistent dictionary stored as JSON using pathlib.
       Automatically saves on each set or delete operation.
    """
    def __init__(self, filename: str):
        super().__init__()
        self.path = Path(filename)
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    self.update(data)
            except Exception:
                pass

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._save()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._save()

    def _save(self):
        self.path.write_text(json.dumps(self, indent=2))

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("You must provide a TELEGRAM_BOT_TOKEN in your .env")

READECK_BASE_URL = os.getenv("READECK_BASE_URL", "http://localhost:8000")
READECK_CONFIG = os.getenv("READECK_CONFIG", None) 
USER_TOKEN_MAP = PersistentDict(".user_tokens.json")

async def start(update: Update, context: CallbackContext) -> None:
    """Send a welcome message and log user ID."""
    user_id = update.effective_user.id
    logger.info(f"User started the bot. user_id={user_id}")
    await update.message.reply_text(
        "Hi! Send me a URL to save it on Readeck.\n\n"
        "You can also specify a title and tags like:\n"
        "https://example.com Interesting Article +news +tech\n\n"
        "To configure your Readeck credentials use one of:\n"
        "• /token <YOUR_READECK_TOKEN>\n"
        "• /register <password>  (your Telegram user ID is used as username)\n\n"
        "After saving a bookmark, I'll give you a custom command like /md_<bookmark_id> "
        "to directly fetch its markdown."
    )

async def help_command(update: Update, context: CallbackContext) -> None:
    """Show help text."""
    await update.message.reply_text(
        "Send me a URL along with an optional title and +labels.\n"
        "Example:\n"
        "https://example.com/article Interesting Article +news +tech\n\n"
        "I will save it to your Readeck account.\n"
        "After saving, I'll show you a command /md_<bookmark_id> to get the article's markdown.\n\n"
        "To set your Readeck credentials use:\n"
        "• /token <YOUR_READECK_TOKEN>\n"
        "or\n"
        "• /register <password>  (your Telegram user ID is used as username)"
    )

async def extract_url_title_labels(text: str):
    """Extract URL, title, and labels from text."""
    url_pattern = r'(https?://[^\s]+)'
    match = re.search(url_pattern, text)
    if not match:
        return None, None, []
    url = match.group(0)
    after_url = text.replace(url, "").strip()
    labels = re.findall(r'\+(\w+)', after_url)
    for lbl in labels:
        after_url = after_url.replace(f"+{lbl}", "")
    title = after_url.strip()
    return url, (title if title else None), labels

async def handle_message(update: Update, context: CallbackContext) -> None:
    """
    Handle non-command text messages:
    - If the message contains a URL, save it as a bookmark.
    - Otherwise, provide guidance.
    """
    user_id = update.effective_user.id
    text = update.message.text.strip()

    token = USER_TOKEN_MAP.get(str(user_id))
    if not token:
        await update.message.reply_text("I don't have your Readeck token. "
                                        "Set it with /token <YOUR_TOKEN> or /register <password>.")
        return

    # Check if the text contains a URL
    if re.search(r'https?://', text):
        url, title, labels = await extract_url_title_labels(text)
        if not url:
            await update.message.reply_text("I couldn't find a valid URL.")
            return
        await save_bookmark(update, url, title, labels, token)
    else:
        await update.message.reply_text(
            "I don't recognize this input.\n"
            "After saving a bookmark, use the provided /md_<bookmark_id> command to view its markdown."
        )

async def register_command(update: Update, context: CallbackContext) -> None:
    """
    Handle the /register command.
    Usage: /register <password>
    Uses the Telegram user ID as the username.
    """
    user_id = update.effective_user.id
    if not context.args:
        username = str(user_id)
        password = str(user_id)
    elif len(context.args) == 1:
        username = str(user_id)
        password = context.args[0]
    elif len(context.args) == 2:
        username = context.args[0]
        password = context.args[1]
    else:
        await update.message.reply_text("Usage: /register <user> <password>\nUsage: /register <password> (your Telegram user ID will be used as username).")
        return
    await register_and_fetch_token(update, username, password)

async def token_command(update: Update, context: CallbackContext) -> None:
    """
    Handle the /token command.
    Usage: /token <YOUR_READECK_TOKEN>
    """
    user_id = update.effective_user.id
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /token <YOUR_READECK_TOKEN>")
        return
    token = context.args[0]
    USER_TOKEN_MAP[str(user_id)] = token
    await update.message.reply_text("Your Readeck token has been saved.")
    logger.info(f"Set token for user_id={user_id}")

async def register_and_fetch_token(update: Update, username: str, password: str):
    """
    Register a new user in Readeck and fetch the corresponding token.
    First, try using the CLI command.
    If it fails, try via Docker.
    Then obtain the token via the API.
    """
    command = ["readeck", "user"] + ['-config', READECK_CONFIG] if READECK_CONFIG else [] +  ["-u", username, "-p", password]
    logger.info(f"Attempting to register user '{username}' using CLI")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"CLI command failed: {result.stderr.strip()}, trying docker")
        docker_command = [
            "docker", "run", "codeberg.org/readeck/readeck:latest",
            "readeck", "user", "-u", username, "-p", password
        ]
        result = subprocess.run(docker_command, capture_output=True, text=True)
        if result.returncode != 0:
            await update.message.reply_text(f"Registration failed: {result.stderr.strip()}")
            logger.error(f"Registration failed with docker: {result.stderr.strip()}")
            return

    logger.info(f"User '{username}' registered successfully. Fetching token...")

    auth_url = f"{READECK_BASE_URL}/api/auth"
    payload = {"application": "telegram bot", "username": username, "password": password}
    headers = {"accept": "application/json", "content-type": "application/json"}
    r = requests.post(auth_url, headers=headers, json=payload)
    if 200 <= r.status_code < 300:
        data = r.json()
        token = data.get("token")
        if token:
            USER_TOKEN_MAP[str(update.effective_user.id)] = token
            await update.message.reply_text("Registration successful! Your token has been saved.")
            logger.info(f"Token for user '{username}' saved for Telegram user {update.effective_user.id}")
        else:
            await update.message.reply_text("Registration succeeded but failed to retrieve token.")
            logger.error("Token missing in auth response.")
    else:
        await update.message.reply_text("Having troubles now... try later.")
        logger.error(f"Failed to fetch token from API: {r.text}")

async def save_bookmark(update: Update, url: str, title: str, labels: list, token: str):
    """Save a bookmark to Readeck and return a link and the bookmark_id."""
    data = {"url": url}
    if title:
        data["title"] = title
    if labels:
        data["labels"] = labels

    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
        "content-type": "application/json",
    }
    try:
        r = requests.post(f"{READECK_BASE_URL}/api/bookmarks", json=data, headers=headers)
    except requests.RequestException as e:
        await update.message.reply_text("Having troubles now... try later.")
        logger.error(f"Error saving bookmark: {e}")
        return

    if r.status_code == 202:
        bookmark_id = r.headers.get("Bookmark-Id")
        if bookmark_id:
            try:
                details = requests.get(f"{READECK_BASE_URL}/api/bookmarks/{bookmark_id}", headers=headers)
            except requests.RequestException as e:
                await update.message.reply_text("Having troubles now... try later.")
                logger.error(f"Error retrieving bookmark details: {e}")
                return
            if details.status_code == 200:
                info = details.json()
                real_title = info.get("title", "No Title")
                href = info.get("href", "")
                if href:
                    message = (
                        f"Saved: [{real_title}]({href})\n\n"
                        f"Use `/md_{bookmark_id}` to view the article's markdown."
                    )
                    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
                else:
                    message = (
                        f"Saved: {real_title}\n\n"
                        f"Use `/md_{bookmark_id}` to view the article's markdown."
                    )
                    await update.message.reply_text(message)
                logger.info(f"Saved bookmark '{real_title}' with ID {bookmark_id}")
            else:
                await update.message.reply_text("Saved bookmark but failed to retrieve details.")
                logger.warning("Saved bookmark but failed to retrieve details.")
        else:
            await update.message.reply_text("Saved bookmark but missing Bookmark-Id header.")
            logger.warning("Saved bookmark but missing Bookmark-Id header.")
    else:
        await update.message.reply_text("Failed to save bookmark.")
        logger.error("Failed to save bookmark.")

async def dynamic_md_handler(update: Update, context: CallbackContext) -> None:
    """
    Handle dynamic commands like /md_<bookmark_id> to fetch markdown.
    """
    text = update.message.text.strip()
    if text.startswith("/md_"):
        bookmark_id = text[len("/md_"):]
        user_id = update.effective_user.id
        token = USER_TOKEN_MAP.get(str(user_id))
        if not token:
            await update.message.reply_text("I don't have your Readeck token. "
                                            "Set it with /token <YOUR_TOKEN> or /register <password>.")
            return
        await fetch_article_markdown(update, bookmark_id, token)
    else:
        await update.message.reply_text(
            "I don't recognize this command.\n"
            "If you want the markdown of a saved article, use /md_<bookmark_id>."
        )

async def send_long_message(update: Update, text: str):
    # Telegram message limit ~4096 characters
    limit = 4000
    for start in range(0, len(text), limit):
        await update.message.reply_text(text[start:start+limit])

async def fetch_article_markdown(update: Update, bookmark_id: str, token: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/epub+zip",
    }
    try:
        r = requests.get(f"{READECK_BASE_URL}/api/bookmarks/{bookmark_id}/article.md", headers=headers)
    except requests.RequestException as e:
        await update.message.reply_text("Having troubles now... try later.")
        logger.error(f"Error fetching article markdown: {e}")
        return

    if r.status_code == 200:
        article_text = r.text
        await send_long_message(update, article_text)
        logger.info(f"Fetched markdown for bookmark {bookmark_id}")
    else:
        await update.message.reply_text("Failed to retrieve the article markdown.")
        logger.error("Failed to retrieve the article markdown.")

def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("register", register_command))
    application.add_handler(CommandHandler("token", token_command))
    application.add_handler(MessageHandler(filters.COMMAND, dynamic_md_handler))
    # Non-command messages (likely bookmarks)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()

if __name__ == "__main__":
    main()
