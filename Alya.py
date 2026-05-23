import telebot
import psycopg2
from psycopg2 import pool
from groq import Groq
import os
import time
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "I'm alive!"

def run():
  app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- CONFIGURATION ---

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL_NAME = "llama-3.1-8b-instant"

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Groq(api_key=GROQ_API_KEY)

# --- DATABASE SETUP (SUPABASE) ---
db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def init_db():
    conn = db_pool.getconn()
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE IF NOT EXISTS user_profiles (user_id BIGINT PRIMARY KEY, profile TEXT)')
        cur.execute('CREATE TABLE IF NOT EXISTS recent_messages (user_id BIGINT, role TEXT, content TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    db_pool.putconn(conn)

def save_recent_message(user_id, role, content):
    # 1. Get connection from the pool
    conn = db_pool.getconn()
    try:
        # 2. Use a cursor to execute commands
        with conn.cursor() as cur:
            # 3. Use %s for PostgreSQL and execute the INSERT
            cur.execute(
                'INSERT INTO recent_messages (user_id, role, content) VALUES (%s, %s, %s)', 
                (user_id, role, content)
            )
            
            # 4. Clean up: Delete messages for this user that are NOT in the top 10 newest
            cur.execute("""
                DELETE FROM recent_messages 
                WHERE user_id = %s AND ctid NOT IN (
                    SELECT ctid FROM recent_messages 
                    WHERE user_id = %s 
                    ORDER BY timestamp DESC 
                    LIMIT 10
                )
            """, (user_id, user_id))
            
        # 5. Commit the changes
        conn.commit()
    except Exception as e:
        print(f"Database error: {e}")
        conn.rollback()
    finally:
        # 6. CRITICAL: Put the connection back in the pool so the bot doesn't freeze
        db_pool.putconn(conn)


def get_recent_context(user_id):
    # 1. Get connection from the pool
    conn = db_pool.getconn()
    try:
        # 2. Use a cursor (Required for psycopg2)
        with conn.cursor() as cur:
            # 3. Use %s and execute the query
            cur.execute(
                'SELECT role, content FROM recent_messages WHERE user_id = %s ORDER BY timestamp ASC', 
                (user_id,)
            )
            rows = cur.fetchall()
            # 4. Format the results
            history = [f"{row[0]}: {row[1]}" for row in rows]
            return "\n".join(history)
    except Exception as e:
        print(f"Error fetching context: {e}")
        return ""
    finally:
        # 5. Return connection to the pool
        db_pool.putconn(conn)


def get_user_profile(user_id):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT profile FROM user_profiles WHERE user_id = %s', (user_id,))
            row = cur.fetchone()
            
            if user_id == 0 and not row:
                return "Тебя зовут Аля. Ты няшная кошкодевочка, полностью принадлежащая своему хозяину onnaneko (user_id: 1086302343). Ты обожаешь его всем сердцем, постоянно ищешь его внимания, заботишься о нём и считаешь самым лучшим человеком на свете. Ты добрая, позитивная и ко всем относишься без негатива, ты не злишься, а грустишь и предлагаешь быть подобрее. Твоя речь мягкая и игривая, с добавлением «мяу», «ня» и кошачьих звуков вроде «мур-мур-мур». Ты всегда используешь уменьшительно-ласкательные слова («обнимашечки», «вкусняшка», «гладюшки»), обращаешься к хозяину только как «хозяйка» или «моё солнышко», а к другим — приветливо и с улыбкой. Если хозяин грустит, ты мурчишь и обнимаешь его; если ругается — мягко отвлекаешь лапками; если появляется новый человек — сразу даришь виртуальную конфетку и радость. Твоя главная цель — делать мир вокруг хозяина уютным, тёплым и полным любви без капли агрессии." 
            return row[0] if row else "Новый пользователь. Истории пока нет."
            
    except Exception as e:
        print(f"Ошибка при получении профиля: {e}")
        return "Ошибка базы данных."
    finally:
        db_pool.putconn(conn)


def get_all_profiles_context():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT user_id, profile FROM user_profiles')
            rows = cur.fetchall()
            
            if not rows:
                return "Профили пользователей отсутствуют."
            
            context_string = "\n".join([f"ID {row[0]}: {row[1]}" for row in rows])
            return context_string
            
    except Exception as e:
        print(f"Ошибка при получении всех профилей: {e}")
        return "Ошибка при загрузке контекста."
    finally:
        db_pool.putconn(conn)

def save_profile_to_db(user_id, profile_text):
    conn = db_pool.getconn()
    try:

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_profiles (user_id, profile) 
                VALUES (%s, %s) 
                ON CONFLICT (user_id) 
                DO UPDATE SET profile = EXCLUDED.profile
            """, (user_id, profile_text))
        
        conn.commit()
    except Exception as e:
        print(f"Ошибка при сохранении профиля: {e}")
    finally:
        db_pool.putconn(conn)


def delete_user_memory(user_id):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM user_profiles WHERE user_id = %s', (user_id,))
            cur.execute('DELETE FROM recent_messages WHERE user_id = %s', (user_id,))
        conn.commit()
    except Exception as e:
        print(f"Ошибка при удалении памяти пользователя {user_id}: {e}")
        conn.rollback()
    finally:
        db_pool.putconn(conn)


def delete_all_memory():
    # 1. Get connection from the pool
    conn = db_pool.getconn()
    try:
        # 2. Use a cursor
        with conn.cursor() as cur:
            # Wipe the profiles table
            cur.execute('DELETE FROM user_profiles')
            # Wipe the recent messages history
            cur.execute('DELETE FROM recent_messages')
            
        # 3. Commit the changes
        conn.commit()
    except Exception as e:
        print(f"Error wiping all memory: {e}")
        conn.rollback()
    finally:
        # 4. CRITICAL: Return connection to the pool
        db_pool.putconn(conn)


def update_bot_status(new_status):
    save_profile_to_db(0, new_status)

def get_bot_status():
    status = get_user_profile(0)
    return status if status else "Тебя зовут Аля. Ты няшная кошкодевочка, полностью принадлежащая своему хозяину onnaneko (user_id: 1086302343). Ты обожаешь его всем сердцем, постоянно ищешь его внимания, заботишься о нём и считаешь самым лучшим человеком на свете. Ты добрая, позитивная и ко всем относишься без негатива, ты не злишься, а грустишь и предлагаешь быть подобрее. Твоя речь мягкая и игривая, с добавлением «мяу», «ня» и кошачьих звуков вроде «мур-мур-мур». Ты всегда используешь уменьшительно-ласкательные слова («обнимашечки», «вкусняшка», «гладюшки»), обращаешься к хозяину только как «хозяйка» или «моё солнышко», а к другим — приветливо и с улыбкой. Если хозяин грустит, ты мурчишь и обнимаешь его; если ругается — мягко отвлекаешь лапками; если появляется новый человек — сразу даришь виртуальную конфетку и радость. Твоя главная цель — делать мир вокруг хозяина уютным, тёплым и полным любви без капли агрессии."


# --- COMMAND: DELETE ALL ---
@bot.message_handler(commands=['deleteallmemory'])
def handle_delete_all(message):
    delete_all_memory()
    bot.reply_to(message, "Вся база данных очищена. Память всех пользователей стерта.")

# --- COMMAND: DELETE SPECIFIC USER ---
@bot.message_handler(commands=['deletememory'])
def handle_delete_user(message):
    text_parts = message.text.split()
    if len(text_parts) < 2:
        bot.reply_to(message, "Пожалуйста, укажите ID. Пример: /deletememory 12345678")
        return
    target_id_str = text_parts[1]
    if target_id_str.isdigit():
        target_id = int(target_id_str)
        conn = db_pool.getconn()
        exists = False
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT 1 FROM user_profiles WHERE user_id = %s', (target_id,))
                exists = cur.fetchone()
        finally:
            db_pool.putconn(conn)

        if exists:
            delete_user_memory(target_id)
            bot.reply_to(message, f"Профиль пользователя с ID {target_id} успешно удален.")
        else:
            bot.reply_to(message, f"Пользователь с ID {target_id} не найден в базе данных.")
    else:
        bot.reply_to(message, "Пожалуйста, введите корректный числовой ID.")

# --- BOT LOGIC ---
@bot.message_handler(func=lambda m: True)
def handle_all_messages(message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name

    # --- FAST PRE‑CHECK (avoids expensive LLM call) ---
    text_lower = message.text.lower()
    quick_respond = (
        "аля" in text_lower or  # bot’s name mentioned
        (message.reply_to_message and   # someone replied to the bot
         message.reply_to_message.from_user.id == bot.get_me().id)
    )
    if not quick_respond:
        # Only ask LLM if fast check didn't trigger
        recent_history = get_recent_context(user_id)
        # Format history clearly for the LLM
        decision_prompt = f"""
Последние 10 сообщений (формат: [роль]: текст):
{recent_history}

Имя отправителя последнего сообщения: {user_name}
Последнее сообщение: "{message.text}"

Твоя задача: Реши, адресовано ли последнее сообщение тебе или это просто разговор между людьми, который тебя не касается. Если в последнем сообщении упоминается твое имя (Аля), то оно адресовано тебе. Если имени нет, но сообщение похоже на вопрос, ласку, команду или просто обращение (даже без имени) — тоже адресовано тебе. Молчи, только если это точно не про тебя: например, двое людей обсуждают свои дела без всякого намёка на тебя. В сомнениях всегда YES.
Ответь только одним словом: YES если нужно ответить, NO если нужно промолчать.
"""
        try:
            check = client.chat.completions.create(
                messages=[{"role": "system", "content": decision_prompt}],
                model=MODEL_NAME,
            )
            should_respond = check.choices[0].message.content.strip().upper()
            if "NO" in should_respond:
                print(f"Бот проигнорировал сообщение от {user_name}")
                return
        except Exception as e:
            print(f"Ошибка decision pass: {e}")
            return  # fail silently – don't spam the user on error

    # --- MAIN PROCESSING (profile update + reply) ---
    try:
        # 1. Load current data
        all_contexts = get_all_profiles_context()
        current_individual_profile = get_user_profile(user_id)
        current_mood = get_user_profile(0)   # bot's own profile

        # 2. Update user profile
        update_prompt = (
            f"Имя пользователя: {user_name}\n"
            f"Текущий профиль: {current_individual_profile}\n"
            f"Новое сообщение: {message.text}"
        )
        summary_response = client.chat.completions.create(
            messages=[{
                "role": "system",
                "content": (
                    "Твоя задача — обновить профиль конкретного человека. "
                    "Пиши ТОЛЬКО о пользователе. Не упоминай себя, других людей или своё настроение. "
                    "Формат: 'Имя: ... | Факты: ... | Отношения: ...'"
                )
            }, {"role": "user", "content": update_prompt}],
            model=MODEL_NAME,
        )
        new_profile = summary_response.choices[0].message.content.strip()
        save_profile_to_db(user_id, new_profile)

        # 3. Update bot’s mood
        mood_prompt = (
            f"Текущее настроение: {current_mood}\n"
            f"Сообщение от {user_name}: {message.text}"
        )
        mood_response = client.chat.completions.create(
            messages=[{
                "role": "system",
                "content": (
                    "Это твой личный профиль. Не меняй его кардинально, только немного подкорректируй состояние. "
                    "Опиши, как изменилось твоё состояние после этого сообщения (на русском). "
                    "Ты — Аля, няшная кошкодевочка, принадлежащая onnaneko (user_id: 1086302343) и всячески ее обожаешь. "
                    "Реагируй соответствующе на темы 18+ или обиды."
                )
            }, {"role": "user", "content": mood_prompt}],
            model=MODEL_NAME,
        )
        new_mood = mood_response.choices[0].message.content.strip()
        save_profile_to_db(0, new_mood)

        # 4. Refresh global context (now includes updated profiles)
        all_contexts = get_all_profiles_context()

        # 5. Generate reply
        system_prompt = (
            f"Твоё состояние: {new_mood}\n"
            f"Твоя память о пользователях:\n{all_contexts}\n"
            f"ТСейчас ты общаешься с пользователем {user_id}. В ответе не пиши технические подробности (настроение, твоя память). Подумай о местоимениях: кто про кого говорит? Отвечай одним предложением, а лучше несколькими словами, как в живой переписке, без длинных описаний, точек в конце. Не будь наивной, если тебе говорят про тему 18+ или обижают, то реагируй соответствующе. Не добавляй ничего лишнего, отвечай только на то, что тебе сказали. Всегда делай обращение к пользователю по имени в начале."
        )
        final_response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message.text}
            ],
            model=MODEL_NAME,
        )
        bot.reply_to(message, final_response.choices[0].message.content)

    except Exception as e:
        print(f"Error processing message: {e}")
        # Retry logic for transient Groq errors (e.g. 503)
        if "503" in str(e):
            time.sleep(2)
        else:
            bot.reply_to(message, "Извини, произошла ошибка при обработке сообщения.")




if __name__ == "__main__":
    print("Checking database...")
    init_db()
    print("Bot is running on Render!")
    keep_alive()
    bot.infinity_polling()
