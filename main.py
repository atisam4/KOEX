from flask import Flask, request, render_template, redirect, url_for, flash, session, jsonify
import requests
import time
import os
from datetime import datetime
from functools import wraps
import logging
from ratelimit import limits, sleep_and_retry
import threading
import queue
import json
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configure logging
logging.basicConfig(
    filename='app.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Message queue for processing
message_queue = queue.Queue()
MAX_WORKERS = 5
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Rate limiting - increased to 300 calls per hour for better performance
CALLS = 300
RATE_LIMIT = 3600

@sleep_and_retry
@limits(calls=CALLS, period=RATE_LIMIT)
def check_rate_limit():
    return

headers = {
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/56.0.2924.76 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Accept-Language': 'en-US,en;q=0.9,fr;q=0.8',
    'referer': 'www.google.com'
}

def validate_input(thread_id, time_interval):
    if not thread_id.strip():
        return False, "Conversation ID cannot be empty"
    try:
        time_val = int(time_interval)
        if time_val < 1 or time_val > 7200:  # Increased max time to 2 hours
            return False, "Time interval must be between 1 and 7200 seconds"
    except ValueError:
        return False, "Invalid time interval"
    return True, ""

def process_message(data):
    try:
        post_url = data['post_url']
        access_token = data['access_token']
        message = data['message']
        haters_name = data['haters_name']

        parameters = {
            'access_token': access_token,
            'message': f"{haters_name} {message}"
        }

        response = requests.post(post_url, json=parameters, headers=headers)
        current_time = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")

        if response.ok:
            logging.info(f"Success - Message sent at {current_time}")
            return True
        else:
            logging.error(f"Failed at {current_time}: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Error processing message: {str(e)}")
        return False

def background_worker():
    while True:
        try:
            if not session.get('is_running', False):
                time.sleep(1)
                continue

            data = message_queue.get()
            if data is None:
                break

            success = process_message(data)
            
            progress = session.get('progress', {})
            if success:
                progress['success'] = progress.get('success', 0) + 1
            else:
                progress['failed'] = progress.get('failed', 0) + 1
            
            progress['last_update'] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
            session['progress'] = progress

            speed = data.get('speed', 60)
            time.sleep(speed)

        except Exception as e:
            logging.error(f"Worker error: {str(e)}")
        finally:
            message_queue.task_done()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/', methods=['POST'])
def send_message():
    try:
        thread_id = request.form.get('threadId')
        mn = request.form.get('kidx')
        time_interval = request.form.get('time')

        is_valid, error_message = validate_input(thread_id, time_interval)
        if not is_valid:
            return error_message, 400

        try:
            check_rate_limit()
        except Exception:
            return 'Rate limit exceeded. Please try again later.', 429

        txt_file = request.files['txtFile']
        access_tokens = txt_file.read().decode().splitlines()

        messages_file = request.files['messagesFile']
        messages = messages_file.read().decode().splitlines()

        if not access_tokens or not messages:
            return 'Files cannot be empty', 400

        num_comments = len(messages)
        max_tokens = len(access_tokens)

        # Create a folder with the Convo ID
        folder_name = f"Convo_{thread_id}"
        os.makedirs(folder_name, exist_ok=True)

        # Save configuration for recovery
        config = {
            'thread_id': thread_id,
            'haters_name': mn,
            'speed': int(time_interval),
            'total_messages': num_comments,
            'start_time': datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        }

        try:
            # Save all files
            files_to_save = {
                "CONVO.txt": thread_id,
                "token.txt": "\n".join(access_tokens),
                "haters.txt": mn,
                "time.txt": str(time_interval),
                "message.txt": "\n".join(messages),
                "np.txt": "NP",
                "config.json": json.dumps(config)
            }

            for filename, content in files_to_save.items():
                with open(os.path.join(folder_name, filename), "w") as f:
                    f.write(content)

        except IOError as e:
            logging.error(f"File operation failed: {str(e)}")
            return 'Failed to save files', 500

        post_url = f'https://graph.facebook.com/v15.0/t_{thread_id}/'
        speed = int(time_interval)

        # Clear existing queue
        while not message_queue.empty():
            message_queue.get()

        # Start worker threads if not already running
        for _ in range(MAX_WORKERS):
            thread = threading.Thread(target=background_worker, daemon=True)
            thread.start()

        # Queue all messages
        for message_index in range(num_comments):
            token_index = message_index % max_tokens
            message_data = {
                'post_url': post_url,
                'access_token': access_tokens[token_index],
                'message': messages[message_index].strip(),
                'haters_name': mn,
                'speed': speed
            }
            message_queue.put(message_data)

        session['is_running'] = True
        session['progress'] = {
            'success': 0,
            'failed': 0,
            'total': num_comments,
            'last_update': datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
            'start_time': datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        }

        return '', 200

    except Exception as e:
        logging.error(f"General error: {str(e)}")
        return str(e), 500

@app.route('/stop')
def stop_process():
    session['is_running'] = False
    return '', 200

@app.route('/progress')
def get_progress():
    return jsonify(session.get('progress', {}))

@app.route('/status')
def get_status():
    progress = session.get('progress', {})
    if not progress:
        return jsonify({'status': 'idle'})
    
    total = progress.get('total', 0)
    success = progress.get('success', 0)
    failed = progress.get('failed', 0)
    completed = success + failed
    
    if completed >= total:
        return jsonify({'status': 'completed'})
    elif session.get('is_running', False):
        return jsonify({'status': 'running'})
    else:
        return jsonify({'status': 'stopped'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
