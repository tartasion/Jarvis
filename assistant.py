"""
Ключ API (https://console.anthropic.com):
    Windows (PowerShell):  setx ANTHROPIC_API_KEY "sk-ant-..."   (потім перезапусти термінал)
    Linux/macOS:           export ANTHROPIC_API_KEY="sk-ant-..."
"""

import base64
import io
import json
import os
import platform
import subprocess
import sys
import threading
from datetime import datetime, timedelta

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import edge_tts
    import asyncio
    import tempfile
    import pygame
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False


# =========================================================
# НАЛАШТУВАННЯ
# =========================================================

# Розумніша:  "claude-sonnet-5-5"  (зараз обрано швидшу нижче)
# Швидша/дешевша (краще для голосу): "claude-haiku-4-5-20251001"
MODEL_NAME = "claude-haiku-4-5-20251001"

MAX_TOKENS = 1024          # максимум токенів однієї відповіді моделі
MAX_AGENT_STEPS = 8        # максимум кроків (викликів інструментів) на одну команду
MAX_HISTORY_TURNS = 8      # скільки останніх реплік користувача пам'ятати

# Вбудований веб-пошук Anthropic. Якщо отримуєш помилку про недоступність
# інструмента — увімкни web search в консолі Anthropic або поставь False.
USE_WEB_SEARCH = True

# ---- ГОЛОС ОЗВУЧЕННЯ ----
USE_NATURAL_VOICE = True
EDGE_TTS_VOICE = "uk-UA-PolinaNeural"   # або "uk-UA-OstapNeural"

SPEECH_LANGUAGE = "uk-UA"
USE_OFFLINE_STT = False
VOSK_MODEL_PATH = "vosk-model"

WORK_DIR = os.path.expanduser("~")

# Підстраховка: якщо в команді/коді є ці фрагменти — спершу питаємо підтвердження.
DANGEROUS_KEYWORDS = [
    "rm ", "del ", "rmdir", "rd ", "remove", "delete", "format",
    "shutdown", "restart", "reboot", "kill", "taskkill", "stop-process",
    "remove-item", "unlink", "rmtree",
]

WAKE_WORDS = ["асистент", "джарвіс", "assistant"]
USE_WAKE_WORD = False

REMINDERS = []
_VOICE_REF = {"voice": None}
_SCREEN_SCALE = {"f": 1.0}   # відношення реального екрана до зменшеного скріншота


# =========================================================
# ГОЛОС
# =========================================================

_pygame_mixer_ready = False


def _speak_edge_tts(text: str) -> bool:
    global _pygame_mixer_ready
    if not EDGE_TTS_AVAILABLE:
        return False

    tmp_path = None
    try:
        tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3").name

        async def _generate():
            communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
            await communicate.save(tmp_path)

        asyncio.run(_generate())

        if not _pygame_mixer_ready:
            pygame.mixer.init()
            _pygame_mixer_ready = True

        pygame.mixer.music.load(tmp_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
        pygame.mixer.music.unload()
        return True
    except Exception as e:
        print(f"Edge TTS не спрацював ({e}), використовую запасний голос.")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def speak_text(text: str):
    text = str(text).strip()
    if not text:
        return
    print(f"{text}")

    if USE_NATURAL_VOICE and _speak_edge_tts(text):
        return

    if pyttsx3 is None:
        return
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", 185)
        engine.say(text)
        engine.runAndWait()
        engine.stop()
        del engine
    except Exception as e:
        print(f"TTS помилка (текст все одно надруковано вище): {e}")


class Voice:
    def __init__(self):
        if sr is None:
            raise RuntimeError("Встанови: pip install SpeechRecognition pyaudio")

        self.recognizer = sr.Recognizer()
        self.mic = sr.Microphone()

        with self.mic as source:
            print("Калібрування мікрофона...")
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)

    def listen(self) -> str:
        with self.mic as source:
            print("Слухаю...")
            try:
                audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=12)
            except sr.WaitTimeoutError:
                return ""

        try:
            text = self.recognizer.recognize_google(audio, language=SPEECH_LANGUAGE)
            print(f"{text}")
            return text.strip()
        except sr.UnknownValueError:
            return ""
        except sr.RequestError as e:
            print(f"Помилка Google Speech: {e}")
            return ""

    def speak(self, text: str):
        speak_text(text)


class OfflineVoice:
    def __init__(self, model_path=VOSK_MODEL_PATH):
        try:
            from vosk import Model, KaldiRecognizer
            import pyaudio
        except ImportError:
            raise RuntimeError("Встанови: pip install vosk pyaudio")

        if not os.path.isdir(model_path):
            raise RuntimeError(f"Не знайдено Vosk модель: {model_path}")

        self.model = Model(model_path)
        self.rec = KaldiRecognizer(self.model, 16000)
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=pyaudio.paInt16, channels=1, rate=16000,
            input=True, frames_per_buffer=4000,
        )
        self.stream.start_stream()

    def listen(self, max_chunks=35) -> str:
        print("Слухаю (офлайн)...")
        self.rec.Reset()
        for _ in range(max_chunks):
            data = self.stream.read(4000, exception_on_overflow=False)
            if self.rec.AcceptWaveform(data):
                text = json.loads(self.rec.Result()).get("text", "").strip()
                if text:
                    print(f"{text}")
                    return text
        return json.loads(self.rec.FinalResult()).get("text", "").strip()

    def speak(self, text: str):
        speak_text(text)


# =========================================================
# ІНСТРУМЕНТИ — кілька універсальних примітивів
# =========================================================
# Модель сама вирішує, як ними скористатися. Наприклад:
#   "зроби тихіше"      -> run_python з pycaw
#   "відкрий калькулятор" -> run_shell("calc") або open_target
#   "що на екрані"      -> screenshot
#   "вимкни комп'ютер"  -> run_shell("shutdown /s /t 5") (з підтвердженням)

def tool_run_shell(command="", **_):
    try:
        if platform.system() == "Windows":
            cmd = ["powershell", "-NoProfile", "-Command", command]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=60, cwd=WORK_DIR, encoding="utf-8", errors="ignore")
        else:
            result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                    timeout=60, cwd=WORK_DIR)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return output[:4000] or f"Виконано (код {result.returncode})."
    except subprocess.TimeoutExpired:
        return "Команда виконується понад 60 секунд і була зупинена."
    except Exception as e:
        return f"Помилка: {e}"


def tool_run_python(code="", **_):
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                timeout=60, cwd=WORK_DIR, encoding="utf-8", errors="ignore")
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return output[:4000] or "Код виконано."
    except subprocess.TimeoutExpired:
        return "Код виконується понад 60 секунд і був зупинений."
    except Exception as e:
        return f"Помилка: {e}"


def tool_open_target(target="", **_):
    """Відкриває URL, файл, папку або програму."""
    import webbrowser
    system = platform.system()
    try:
        if target.startswith(("http://", "https://")):
            webbrowser.open(target)
        elif system == "Windows":
            os.startfile(target)
        elif system == "Darwin":
            subprocess.Popen(["open", target] if os.path.exists(target) else ["open", "-a", target])
        else:
            subprocess.Popen(["xdg-open", target] if os.path.exists(target) else [target])
        return f"Відкрито: {target}"
    except Exception as e:
        return f"Не вдалося відкрити '{target}': {e}"


def tool_screenshot(**_):
    """Повертає скріншот як image-блок — модель бачить його сама."""
    try:
        import pyautogui
    except ImportError:
        return "Встанови pyautogui: pip install pyautogui"

    img = pyautogui.screenshot()
    max_w = 1280
    if img.width > max_w:
        _SCREEN_SCALE["f"] = img.width / max_w
        img = img.resize((max_w, int(img.height * max_w / img.width)))
    else:
        _SCREEN_SCALE["f"] = 1.0

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("utf-8")
    return [{
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }]


def tool_computer(action="", x=None, y=None, button="left", text="", **_):
    try:
        import pyautogui
    except ImportError:
        return "Встанови pyautogui: pip install pyautogui"

    f = _SCREEN_SCALE["f"]
    try:
        if action in ("move", "click", "double_click") and x is not None and y is not None:
            pyautogui.moveTo(int(float(x) * f), int(float(y) * f), duration=0.1)

        if action == "move":
            return "Курсор переміщено."
        if action == "click":
            pyautogui.click(button=button)
            return f"Клік ({button})."
        if action == "double_click":
            pyautogui.doubleClick()
            return "Подвійний клік."
        if action == "type":
            pyautogui.write(text, interval=0.01)
            return "Текст надруковано."
        if action == "key":
            keys = [k.strip() for k in text.lower().split("+")]
            if len(keys) > 1:
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(keys[0])
            return f"Натиснуто: {text}"
        if action == "scroll":
            pyautogui.scroll(int(float(text or 0)))
            return "Прокручено."
        return f"Невідома дія: {action}"
    except Exception as e:
        return f"Помилка керування: {e}"


def tool_clipboard(action="read", text="", **_):
    try:
        import pyperclip
        if action == "copy":
            pyperclip.copy(text)
            return "Скопійовано в буфер."
        content = pyperclip.paste()
        return content if content else "Буфер обміну порожній."
    except Exception as e:
        return f"Помилка буфера: {e}"


def tool_reminder(action="set", seconds=60, message="", **_):
    if action == "list":
        if not REMINDERS:
            return "Активних нагадувань немає."
        return "\n".join(f"{r['time']} — {r['message']}" for r in REMINDERS)

    try:
        secs = float(seconds)
    except Exception:
        secs = 60.0

    entry = {
        "_id": id(object()),
        "message": message,
        "time": (datetime.now() + timedelta(seconds=secs)).strftime("%H:%M:%S"),
    }
    REMINDERS.append(entry)

    def fire():
        voice = _VOICE_REF.get("voice")
        text = f"Нагадування: {message}"
        if voice:
            voice.speak(text)
        else:
            print(text)
        REMINDERS[:] = [r for r in REMINDERS if r.get("_id") != entry["_id"]]

    timer = threading.Timer(secs, fire)
    timer.daemon = True
    timer.start()
    return f"Нагадування встановлено на {entry['time']}: {message}"


TOOL_FUNCS = {
    "run_shell": tool_run_shell,
    "run_python": tool_run_python,
    "open_target": tool_open_target,
    "screenshot": tool_screenshot,
    "computer": tool_computer,
    "clipboard": tool_clipboard,
    "reminder": tool_reminder,
}

DANGEROUS_FLAG_SCHEMA = {
    "type": "boolean",
    "description": "true, якщо дія може видалити/змінити дані, вимкнути чи перезавантажити ПК "
                   "або є незворотною. Тоді користувача буде попрошено підтвердити.",
}

TOOLS = [
    {
        "name": "run_shell",
        "description": (
            "Виконати команду в терміналі користувача (PowerShell на Windows, shell на Linux/macOS). "
            "Використовуй для: запуску програм, файлів і папок, інформації про систему, мережі, "
            "процесів, вимкнення/перезавантаження ПК тощо."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "dangerous": DANGEROUS_FLAG_SCHEMA,
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Виконати Python-код (тим самим інтерпретатором, що й асистент). Використовуй, коли "
            "зручніше за shell: гучність (pycaw), яскравість (screen_brightness_control), "
            "обробка файлів, обчислення, HTTP-запити. Результат бери з print()."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "dangerous": DANGEROUS_FLAG_SCHEMA,
            },
            "required": ["code"],
        },
    },
    {
        "name": "open_target",
        "description": "Відкрити URL (https://...), файл, папку або програму за назвою/шляхом.",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Зробити скріншот екрана й побачити його. Використовуй, коли користувач питає, що "
            "на екрані, або перед кліками мишею, щоб дізнатися координати."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "computer",
        "description": (
            "Керування мишею й клавіатурою. Координати x,y — у системі останнього скріншота. "
            "action: move | click | double_click | type | key | scroll. "
            "Для type передай text. Для key передай text, напр. 'enter' або 'ctrl+c'. "
            "Для scroll text — число (додатне вгору, від'ємне вниз)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["move", "click", "double_click", "type", "key", "scroll"]},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "clipboard",
        "description": "Прочитати (action=read) або записати (action=copy, text=...) буфер обміну.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "copy"]},
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "reminder",
        "description": (
            "Нагадування. action=set — встановити (seconds = через скільки секунд, "
            "message = текст), action=list — показати активні."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "list"]},
                "seconds": {"type": "number"},
                "message": {"type": "string"},
            },
            "required": ["action"],
        },
    },
]

if USE_WEB_SEARCH:
    # Серверний інструмент: пошук виконується на стороні Anthropic.
    TOOLS.append({"type": "web_search_20250305", "name": "web_search", "max_uses": 3})


def build_system_prompt() -> str:
    return f"""Ти JARVIS — голосовий асистент на комп'ютері користувача.

Твої відповіді озвучуються вголос, тому:
- відповідай українською (або мовою користувача), дуже коротко, 1–2 речення;
- жодного markdown, списків, емодзі, посилань і довгих шляхів — тільки звичайна мова;
- не пояснюй свої міркування.

Ти сам визначаєш, чого хоче користувач, і вирішуєш, що робити. Якщо потрібна дія на ПК —
виконай її інструментами (можна кількома кроками), не питаючи дозволу на очевидне.
Якщо запит — просто питання чи розмова, відповідай без інструментів.
Перед тривалою дією можеш коротко сказати, що робиш.
Після дії підтверди результат одним коротким реченням; якщо щось не вийшло — скажи чесно.
Інформацію (погода, новини, факти) шукай через web_search, не вигадуй.
Для потенційно небезпечних дій став dangerous=true — система спитає підтвердження.
Якщо голосове розпізнавання, схоже, спотворило слово — здогадайся за змістом.

Середовище: {platform.system()} {platform.release()}, домашня папка: {WORK_DIR}
Поточний час: {datetime.now().strftime('%Y-%m-%d %H:%M, %A')}
"""


# =========================================================
# ХМАРНА МОДЕЛЬ + АГЕНТНИЙ ЦИКЛ
# =========================================================

_client = None


def get_client():
    global _client
    if _client is None:
        if anthropic is None:
            raise RuntimeError("Встанови: pip install anthropic")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("Не задано змінну середовища ANTHROPIC_API_KEY.")
        _client = anthropic.Anthropic(timeout=60.0, max_retries=2)
    return _client


def confirm(voice, question):
    voice.speak(question + " Скажи «так» для підтвердження.")
    try:
        answer = voice.listen().lower()
    except Exception:
        answer = ""
    if not answer:
        answer = input("Підтвердити дію? (так/ні): ").strip().lower()
    return answer in ("так", "yes", "ok", "окей", "підтверджую", "давай")


def execute_tool(voice, block) -> dict:
    """Виконує один tool_use блок і повертає tool_result."""
    name = block.name
    args = block.input or {}
    func = TOOL_FUNCS.get(name)

    if func is None:
        return {"type": "tool_result", "tool_use_id": block.id,
                "content": f"Невідомий інструмент: {name}", "is_error": True}

    print(f"{name}({json.dumps(args, ensure_ascii=False)[:200]})")

    # Підстраховка безпеки: прапорець від моделі + перевірка за ключовими словами.
    if name in ("run_shell", "run_python"):
        body = (args.get("command") or args.get("code") or "").lower()
        if args.get("dangerous") or any(k in body for k in DANGEROUS_KEYWORDS):
            short = body[:120].replace("\n", " ")
            if not confirm(voice, f"Потенційно небезпечна дія: {short}."):
                return {"type": "tool_result", "tool_use_id": block.id,
                        "content": "Користувач скасував дію."}

    try:
        result = func(**args)
    except Exception as e:
        return {"type": "tool_result", "tool_use_id": block.id,
                "content": f"Помилка: {e}", "is_error": True}

    print(f"   -> {result if isinstance(result, str) else '[зображення]'}"[:300])
    return {"type": "tool_result", "tool_use_id": block.id, "content": result}


def run_agent(voice, messages):
    """
    Віддає історію хмарній моделі й виконує інструменти, які вона просить,
    поки вона не закінчить. Усі текстові відповіді моделі озвучуються.
    """
    client = get_client()

    for _ in range(MAX_AGENT_STEPS):
        start = datetime.now()
        resp = client.messages.create(
            model=MODEL_NAME,
            max_tokens=MAX_TOKENS,
            system=build_system_prompt(),
            tools=TOOLS,
            messages=messages,
        )
        print(f"⚡ Модель: {(datetime.now() - start).total_seconds():.2f} сек.")

        messages.append({"role": "assistant", "content": resp.content})

        for block in resp.content:
            if block.type == "text" and block.text.strip():
                voice.speak(block.text)

        # Серверний інструмент (web_search) може ненадовго призупинити хід.
        if resp.stop_reason == "pause_turn":
            continue
        if resp.stop_reason != "tool_use":
            return

        results = [execute_tool(voice, b) for b in resp.content if b.type == "tool_use"]
        messages.append({"role": "user", "content": results})

    voice.speak("Я зробив забагато кроків і зупинився. Скажи, що робити далі.")


def compact_history(messages):
    """
    1) Замінює старі скріншоти текстовою позначкою (економія токенів).
    2) Обрізає історію до останніх MAX_HISTORY_TURNS реплік користувача,
       не розриваючи пари tool_use/tool_result.
    """
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for item in m["content"]:
                if isinstance(item, dict) and item.get("type") == "tool_result" \
                        and isinstance(item.get("content"), list):
                    item["content"] = "[скріншот]"

    user_turn_idx = [
        i for i, m in enumerate(messages)
        if m["role"] == "user" and isinstance(m["content"], str)
    ]
    if len(user_turn_idx) > MAX_HISTORY_TURNS:
        del messages[:user_turn_idx[-MAX_HISTORY_TURNS]]


# =========================================================
# MAIN
# =========================================================

def main():
    print("=" * 60)
    print("JARVIS 3.0 — хмарна LLM")
    print(f"Модель: {MODEL_NAME}")
    print("=" * 60)

    get_client()  # одразу перевіряємо ключ і бібліотеку

    voice = OfflineVoice() if USE_OFFLINE_STT else Voice()
    _VOICE_REF["voice"] = voice

    messages = []
    voice.speak("Асистент готовий.")

    while True:
        try:
            text = voice.listen()
            if not text:
                continue

            lowered = text.lower()

            if USE_WAKE_WORD and not any(w in lowered for w in WAKE_WORDS):
                continue

            if any(w in lowered for w in ["вийти", "завершити роботу", "exit", "quit"]):
                voice.speak("До зустрічі!")
                break

            total_start = datetime.now()
            messages.append({"role": "user", "content": text})

            try:
                run_agent(voice, messages)
            except Exception as e:
                # Не залишаємо в історії «повисле» повідомлення без відповіді.
                while messages and not (messages[-1]["role"] == "assistant"
                                        and not any(getattr(b, "type", "") == "tool_use"
                                                    for b in messages[-1]["content"])):
                    messages.pop()
                print(f"Помилка хмарної моделі: {e}")
                voice.speak("Не вдалося зв'язатися з моделлю.")

            compact_history(messages)
            print(f"Загальний час: {(datetime.now() - total_start).total_seconds():.2f} сек.")

        except KeyboardInterrupt:
            print("\nЗавершення роботи.")
            break
        except Exception as e:
            print(f"Неочікувана помилка: {e}")


if __name__ == "__main__":
    main()