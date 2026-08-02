from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import edge_tts
import asyncio
import os
import uuid
import re
import json
from datetime import datetime


# =========================================================
# AI VOICE STUDIO PRO - ONLINE VERSION
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OUTPUT_FOLDER = os.path.join(
    BASE_DIR,
    "generated_audio"
)

HISTORY_FILE = os.path.join(
    BASE_DIR,
    "audio_history.json"
)

os.makedirs(
    OUTPUT_FOLDER,
    exist_ok=True
)


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)

CORS(app)


# =========================================================
# SAFE FILE NAME
# =========================================================

def safe_filename(name):

    name = (name or "").strip()

    if not name:
        name = "AI-Voice"

    name = re.sub(
        r'[<>:"/\\|?*]',
        "",
        name
    )

    name = re.sub(
        r"\s+",
        "-",
        name
    )

    return name[:80]


# =========================================================
# HISTORY
# =========================================================

def load_history():

    if not os.path.exists(HISTORY_FILE):
        return []

    try:

        with open(
            HISTORY_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

            if isinstance(data, list):
                return data

            return []

    except Exception as error:

        print(
            "HISTORY LOAD ERROR:",
            repr(error)
        )

        return []


def save_history(history):

    try:

        with open(
            HISTORY_FILE,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                history,
                file,
                indent=2,
                ensure_ascii=False
            )

    except Exception as error:

        print(
            "HISTORY SAVE ERROR:",
            repr(error)
        )


def add_history(item):

    history = load_history()

    history.insert(
        0,
        item
    )

    history = history[:50]

    save_history(
        history
    )


# =========================================================
# TEXT PROCESSING
# =========================================================

def prepare_text(text, style):

    text = (text or "").strip()

    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    if style == "horror":

        text = text.replace(
            "...",
            "…"
        )

    elif style == "emotional":

        text = re.sub(
            r"\.\s+",
            ". ",
            text
        )

    elif style == "kids":

        text = re.sub(
            r"!+",
            "!",
            text
        )

    return text


# =========================================================
# LONG SCRIPT PROCESSING
# =========================================================

def split_text(
    text,
    max_chars=2800
):

    paragraphs = text.split("\n")

    chunks = []

    current = ""

    for paragraph in paragraphs:

        paragraph = paragraph.strip()

        if not paragraph:
            continue

        if (
            len(current)
            + len(paragraph)
            + 1
            <= max_chars
        ):

            if current:
                current += "\n"

            current += paragraph

        else:

            if current:
                chunks.append(current)

            if len(paragraph) > max_chars:

                sentences = re.split(
                    r"(?<=[.!?])\s+",
                    paragraph
                )

                temp = ""

                for sentence in sentences:

                    if (
                        len(temp)
                        + len(sentence)
                        + 1
                        <= max_chars
                    ):

                        if temp:
                            temp += " "

                        temp += sentence

                    else:

                        if temp:
                            chunks.append(temp)

                        temp = sentence

                current = temp

            else:

                current = paragraph

    if current:
        chunks.append(current)

    return chunks


# =========================================================
# HOME PAGE
# =========================================================

@app.route("/")
def home():

    index_file = os.path.join(
        BASE_DIR,
        "index.html"
    )

    if not os.path.exists(index_file):

        return (
            "ERROR: index.html was not found.",
            404
        )

    return send_file(
        index_file
    )


# =========================================================
# HEALTH CHECK
# Online hosting के लिए
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "success": True,
        "status": "online",
        "service": "AI Voice Studio Pro"
    })


# =========================================================
# VOICE LIBRARY
# =========================================================

@app.route("/voices")
def voices():

    try:

        async def get_voices():

            return await edge_tts.list_voices()

        voice_list = asyncio.run(
            get_voices()
        )

        allowed_locales = [

            "en-US",
            "en-GB",
            "en-AU",
            "en-IN",

            "hi-IN",
            "mr-IN",

            "ta-IN",
            "te-IN",
            "bn-IN",

            "gu-IN",
            "kn-IN",
            "ml-IN"
        ]

        result = []

        for voice_item in voice_list:

            locale = voice_item.get(
                "Locale",
                ""
            )

            if locale not in allowed_locales:
                continue

            result.append({

                "name":
                    voice_item.get(
                        "ShortName",
                        ""
                    ),

                "display_name":
                    voice_item.get(
                        "FriendlyName",
                        voice_item.get(
                            "ShortName",
                            ""
                        )
                    ),

                "locale":
                    locale,

                "gender":
                    voice_item.get(
                        "Gender",
                        "Unknown"
                    )
            })

        result.sort(
            key=lambda item: (
                item["locale"],
                item["gender"],
                item["name"]
            )
        )

        return jsonify({

            "success": True,

            "count":
                len(result),

            "voices":
                result
        })

    except Exception as error:

        print(
            "VOICE LIBRARY ERROR:",
            repr(error)
        )

        return jsonify({

            "success": False,

            "error":
                str(error)

        }), 500


# =========================================================
# GENERATE VOICE
# =========================================================

@app.route(
    "/generate",
    methods=["POST"]
)
def generate():

    try:

        text = request.form.get(
            "text",
            ""
        ).strip()

        voice = request.form.get(
            "voice",
            "en-US-GuyNeural"
        )

        rate = request.form.get(
            "rate",
            "+0%"
        )

        pitch = request.form.get(
            "pitch",
            "+0Hz"
        )

        style = request.form.get(
            "style",
            "natural"
        )

        custom_name = request.form.get(
            "filename",
            "AI-Voice"
        )

        # ---------------------------------------------
        # VALIDATION
        # ---------------------------------------------

        if not text:

            return jsonify({

                "success": False,

                "error":
                    "Please enter some text."

            }), 400

        if len(text) > 50000:

            return jsonify({

                "success": False,

                "error":
                    "Maximum 50,000 characters are allowed."

            }), 400

        if not voice:

            return jsonify({

                "success": False,

                "error":
                    "Please select a voice."

            }), 400

        # ---------------------------------------------
        # PROCESS TEXT
        # ---------------------------------------------

        processed_text = prepare_text(
            text,
            style
        )

        # ---------------------------------------------
        # FILE NAME
        # ---------------------------------------------

        safe_name = safe_filename(
            custom_name
        )

        unique_id = str(
            uuid.uuid4()
        )[:8]

        server_filename = (
            safe_name
            + "-"
            + unique_id
            + ".mp3"
        )

        output_file = os.path.join(
            OUTPUT_FOLDER,
            server_filename
        )

        # ---------------------------------------------
        # LONG SCRIPT
        # ---------------------------------------------

        chunks = split_text(
            processed_text
        )

        final_text = "\n\n".join(
            chunks
        )

        print("")
        print("==============================")
        print("AI VOICE GENERATION")
        print("==============================")
        print("Voice:", voice)
        print("Style:", style)
        print("Rate:", rate)
        print("Pitch:", pitch)
        print("Characters:", len(text))

        # ---------------------------------------------
        # EDGE TTS
        # ---------------------------------------------

        async def generate_audio():

            communicate = edge_tts.Communicate(

                text=final_text,

                voice=voice,

                rate=rate,

                pitch=pitch
            )

            await communicate.save(
                output_file
            )

        asyncio.run(
            generate_audio()
        )

        # ---------------------------------------------
        # CHECK AUDIO
        # ---------------------------------------------

        if not os.path.exists(
            output_file
        ):

            raise Exception(
                "Audio file was not created."
            )

        if os.path.getsize(output_file) == 0:

            raise Exception(
                "Generated audio file is empty."
            )

        # ---------------------------------------------
        # HISTORY
        # ---------------------------------------------

        created_at = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        history_item = {

            "id":
                unique_id,

            "filename":
                server_filename,

            "title":
                safe_name,

            "voice":
                voice,

            "style":
                style,

            "created_at":
                created_at,

            "characters":
                len(text),

            "audio_url":
                "/audio/"
                + server_filename,

            "download_url":
                "/download/"
                + server_filename
        }

        add_history(
            history_item
        )

        print(
            "AUDIO CREATED:",
            output_file
        )

        return jsonify({

            "success": True,

            "audio_url":
                history_item[
                    "audio_url"
                ],

            "download_url":
                history_item[
                    "download_url"
                ],

            "history_item":
                history_item
        })

    except Exception as error:

        print(
            "GENERATION ERROR:",
            repr(error)
        )

        return jsonify({

            "success": False,

            "error":
                str(error)

        }), 500


# =========================================================
# HISTORY
# =========================================================

@app.route("/history")
def history():

    return jsonify({

        "success": True,

        "history":
            load_history()
    })


# =========================================================
# CLEAR HISTORY
# =========================================================

@app.route(
    "/history/clear",
    methods=["POST"]
)
def clear_history():

    save_history(
        []
    )

    return jsonify({
        "success": True
    })


# =========================================================
# PLAY AUDIO
# =========================================================

@app.route(
    "/audio/<filename>"
)
def audio(filename):

    filename = os.path.basename(
        filename
    )

    file_path = os.path.join(
        OUTPUT_FOLDER,
        filename
    )

    if not os.path.exists(
        file_path
    ):

        return (
            "Audio file not found.",
            404
        )

    return send_file(

        file_path,

        mimetype="audio/mpeg"
    )


# =========================================================
# DOWNLOAD AUDIO
# =========================================================

@app.route(
    "/download/<filename>"
)
def download(filename):

    filename = os.path.basename(
        filename
    )

    file_path = os.path.join(
        OUTPUT_FOLDER,
        filename
    )

    if not os.path.exists(
        file_path
    ):

        return (
            "Audio file not found.",
            404
        )

    return send_file(

        file_path,

        mimetype="audio/mpeg",

        as_attachment=True,

        download_name=filename
    )


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(404)
def page_not_found(error):

    return jsonify({
        "success": False,
        "error": "Page not found."
    }), 404


@app.errorhandler(500)
def internal_error(error):

    return jsonify({
        "success": False,
        "error": "Internal server error."
    }), 500


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    print("")
    print("====================================")
    print("       AI VOICE STUDIO PRO")
    print("          ONLINE VERSION")
    print("====================================")
    print("")
    print(
        "Server Port:",
        port
    )
    print("")

    app.run(

        host="0.0.0.0",

        port=port,

        debug=False
    )