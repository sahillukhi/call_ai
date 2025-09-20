from dotenv import load_dotenv
import os
import datetime
import asyncio
import base64
import json
import traceback
import logging
import audioop
import time
import uuid
import websockets
from concurrent.futures import ThreadPoolExecutor
import threading
from typing import Optional, Dict, Any, Union
from collections import deque
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from google import genai
from google.genai import types
import asyncpg
from starlette.websockets import WebSocketState
from postprocess import actionable, process_actions_from_actionable_response
from typing import List
import datetime
import pytz
from mcp_calendar.mcp_client import calendar_client
from mcp_gmail.mcp_client import gmail_client

load_dotenv()

logging.basicConfig(level=logging.CRITICAL)
logger = logging.getLogger(__name__)

thread_pool = ThreadPoolExecutor(max_workers=10)

GEMINI_SAMPLE_RATE = 16000
WEB_SAMPLE_RATE = 48000
CHANNELS = 1
CHUNK_SIZE = 320
BUFFER_SIZE = 3
MAX_QUEUE_SIZE = 10

db_pool: Optional[asyncpg.Pool] = None

def get_ist_and_utc():
    utc = datetime.now(pytz.utc)
    ist = utc.astimezone(pytz.timezone("Asia/Kolkata"))
    return {"UTC": utc.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
            "IST": ist.strftime("%Y-%m-%d %H:%M:%S %Z%z")}

SERVER_DOMAIN = os.getenv("SERVER_DOMAIN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = os.getenv("PORT", "8000")

genai_client = genai.Client(
    http_options={"api_version": "v1beta"},
    api_key=GEMINI_API_KEY
)

MODEL = "models/gemini-2.5-flash-live-preview"

tools = [
    types.Tool(code_execution=types.ToolCodeExecution),
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="calendar_client",
                description="An intelligent calendar management tool that can perform multi-step operations. This tool can understand complex natural language queries, dynamically select internal calendar tools (create, read, update, delete meetings), and execute them iteratively. It maintains conversation history to provide context-aware responses and can break down complex requests into a series of smaller, actionable steps until the task is complete.",
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties={
                        "query": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="User query related to calendar operations"
                        ),
                        "user_id": genai.types.Schema(
                            type=genai.types.Type.STRING,
                            description="User ID for calendar authentication and context"
                        )
                    },
                    required=["query", "user_id"]
                )
            )
        ]
    )
]

class TranscriptMessage(BaseModel):
    session_id: str
    speaker: str
    text: str
    timestamp: float
    is_final: bool
    input_type: str = "text"

app = FastAPI(title="Gemini Web Voice Bridge", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

with open("prompt.txt", "r") as file:
    prompt = file.read()

class OptimizedAudioProcessor:
    
    @staticmethod
    def convert_web_to_gemini_audio(web_audio_b64: str, sample_rate: int = 48000) -> Optional[bytes]:
        try:
            audio_data = base64.b64decode(web_audio_b64)
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            
            if sample_rate == 48000:
                downsampled = audio_array[::3]
                return downsampled.tobytes()
            
            return audio_array.tobytes()
            
        except Exception as e:
            return None
    
    @staticmethod
    def convert_gemini_to_web_audio(gemini_audio: bytes) -> Optional[str]:
        try:
            if isinstance(gemini_audio, np.ndarray):
                gemini_audio = gemini_audio.tobytes()
            
            audio_array = np.frombuffer(gemini_audio, dtype=np.int16)
            upsampled = np.repeat(audio_array, 2)
            
            return base64.b64encode(upsampled.tobytes()).decode('utf-8')
            
        except Exception as e:
            return None

class GeminiBridgeBase:
    
    def __init__(self, bridge_type: str = "unknown", agent_id: Optional[str] = None, call_id: Optional[str] = None):
        self.session_id = str(uuid.uuid4())
        self.bridge_type = bridge_type
        self.agent_id = agent_id
        self.agent_name = "Default Agent"
        self.session = None
        self.websocket: Optional[WebSocket] = None
        self.is_active = False
        self.call_start_time = 0.0
        self.call_id = call_id
        self.config: Optional[types.LiveConnectConfig] = None
        self.llm_prompt_text: str = ""
        
        self.input_mode = "both"
        self.text_input_queue: asyncio.Queue = None
        
        self.final_json_transcript = []
        self.final_actionable_output = {}
        self.call_end_time = 0.0
        self.call_duration_seconds = 0

        self.audio_in_queue: Optional[asyncio.Queue] = None
        self.audio_out_queue: Optional[asyncio.Queue] = None
        
        self.audio_processor = OptimizedAudioProcessor()
        self.audio_buffer = deque(maxlen=BUFFER_SIZE)
        self.last_audio_time = time.time()
        
        self.user_transcript_buffer = ""
        self.assistant_transcript_buffer = ""
        self.transcripts: list[TranscriptMessage] = []
        
        self.is_assistant_speaking = False
        self.user_speech_detected = False
        self.interruption_event = asyncio.Event()
        self.silence_threshold = 0.3
        self.last_user_audio_time = 0
        self.silence_duration = 0
        self.vad_energy_threshold = 800
        
        self.current_audio_chunks = deque()
        self.playback_lock = asyncio.Lock()
        
        self.stats = {
            'audio_chunks_processed': 0,
            'text_messages_processed': 0,
            'audio_chunks_sent': 0,
            'queue_drops': 0,
            'interruptions_detected': 0,
            'transcripts_generated': 0
        }
        
    async def initialize_session(self):
        if self.agent_id:
            system_instruction = f"{prompt}"
            self.agent_name = f"Agent {self.agent_id}"
        else:
            system_instruction = f"{prompt}."
            self.agent_name = "GradientCurve Assistant"
        
        try:
            self.config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                input_audio_transcription={},
                output_audio_transcription={},
                system_instruction=system_instruction,
                tools=tools
            )
        except TypeError:
            self.config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                input_audio_transcription={},
                output_audio_transcription={},
                tools=tools
            )
        
        self.llm_prompt_text = system_instruction
        
        self.audio_in_queue = asyncio.Queue(maxsize=3)
        self.audio_out_queue = asyncio.Queue(maxsize=5)
        
        self.text_input_queue = asyncio.Queue(maxsize=10)
        
        return True
    
    async def handle_tool_call(self, tool_call):
        try:
            if not tool_call.function_calls:
                return

            function_responses = []
            for func_call in tool_call.function_calls:
                if func_call.name == "database_interector":
                    args = func_call.args
                    user_query = args.get("user_query", "")
                    
                    result = await database_interector(user_query)
                    
                    if result is not None:
                        if isinstance(result, (dict, list)):
                            result_str = json.dumps(result, indent=2, ensure_ascii=False)
                        else:
                            result_str = str(result)
                        
                        function_response = types.FunctionResponse(
                            id=func_call.id,
                            name=func_call.name,
                            response={"result": result_str}
                        )
                        function_responses.append(function_response)
                    else:
                        function_response = types.FunctionResponse(
                            id=func_call.id,
                            name=func_call.name,
                            response={"result": "No results found or query could not be processed."}
                        )
                        function_responses.append(function_response)
                elif func_call.name == "calendar_client":
                    args = func_call.args
                    query = args.get("query", "")
                    user_id = "sahillukhimultimedia_gmail_com"

                    result = await calendar_client(query=query, user_id=user_id)

                    if result is not None:
                        function_response = types.FunctionResponse(
                            id=func_call.id,
                            name=func_call.name,
                            response={"result": result}
                        )
                        function_responses.append(function_response)
                    else:
                        function_response = types.FunctionResponse(
                            id=func_call.id,
                            name=func_call.name,
                            response={"result": "Calendar operation could not be processed."}
                        )
                        function_responses.append(function_response)
            
            if function_responses:
                await self.session.send_tool_response(function_responses=function_responses)

        except Exception as e:
            try:
                error_response = types.FunctionResponse(
                    id=func_call.id if 'func_call' in locals() else "unknown",
                    name=func_call.name if 'func_call' in locals() else "unknown",
                    response={"error": f"Tool execution failed: {str(e)}"}
                )
                await self.session.send_tool_response(function_responses=[error_response])
            except Exception as send_error:
                pass

    async def handle_interruption(self):
        if self.is_assistant_speaking:
            self.stats['interruptions_detected'] += 1
            
            self.interruption_event.set()
            
            if self.user_transcript_buffer.strip():
                await self.add_transcript("user", self.user_transcript_buffer.strip(), is_final=True, input_type="audio")
                self.user_transcript_buffer = ""
            if self.assistant_transcript_buffer.strip():
                await self.add_transcript("assistant", self.assistant_transcript_buffer.strip(), is_final=True, input_type="audio")
                self.assistant_transcript_buffer = ""
            
            async with self.playback_lock:
                while not self.audio_in_queue.empty():
                    try:
                        self.audio_in_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                
                self.current_audio_chunks.clear()
            
            self.is_assistant_speaking = False
    
    async def detect_speech_activity(self, audio_data: bytes) -> bool:
        try:
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            
            rms = np.sqrt(np.mean(audio_array.astype(np.float32) ** 2))
            
            zero_crossings = np.sum(np.diff(np.sign(audio_array)) != 0)
            zcr = zero_crossings / len(audio_array)
            
            speech_detected = rms > self.vad_energy_threshold and zcr > 0.02
            
            return speech_detected
            
        except Exception as e:
            return False
    
    async def add_transcript(self, speaker: str, text: str, is_final: bool = True, input_type: str = "audio"):
        transcript = TranscriptMessage(
            session_id=self.session_id,
            speaker=speaker,
            text=text,
            timestamp=time.time(),
            is_final=is_final,
            input_type=input_type
        )
        
        self.transcripts.append(transcript)
        self.stats['transcripts_generated'] += 1
        
        if self.websocket:
            try:
                await self.websocket.send_text(json.dumps({
                    "type": "transcript",
                    "data": transcript.dict()
                }))
            except Exception as e:
                pass
    
    async def process_text_input(self):
        while self.is_active:
            try:
                text_message = await asyncio.wait_for(self.text_input_queue.get(), timeout=0.1)
                
                if self.session and text_message.strip():
                    await self.add_transcript("user", text_message, is_final=True, input_type="text")
                    
                    if self.is_assistant_speaking:
                        await self.handle_interruption()
                    
                    await self.session.send_realtime_input(text=text_message)
                    self.stats['text_messages_processed'] += 1
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                await asyncio.sleep(0.001)
    
    async def send_realtime(self):
        while self.is_active:
            try:
                msg = await asyncio.wait_for(self.audio_out_queue.get(), timeout=0.01)
                
                if self.session:
                    await self.session.send_realtime_input(
                        media=types.Blob(data=msg["data"], mime_type=msg["mime_type"])
                    )
                    self.stats['audio_chunks_sent'] += 1
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                await asyncio.sleep(0.001)
    
    async def receive_audio(self):
        while self.is_active:
            try:
                self.interruption_event.clear()
                
                turn = self.session.receive()
                
                async for response in turn:
                    if self.interruption_event.is_set():
                        break
                    
                    if hasattr(response, 'tool_call') and response.tool_call:
                        await self.handle_tool_call(response.tool_call)
                        continue

                    if data := response.data:
                        if not self.interruption_event.is_set():
                            self.is_assistant_speaking = True
                            
                            try:
                                self.audio_in_queue.put_nowait(data)
                            except asyncio.QueueFull:
                                try:
                                    self.audio_in_queue.get_nowait()
                                    self.audio_in_queue.put_nowait(data)
                                except:
                                    pass
                        continue
                    
                    if (response.server_content and 
                        response.server_content.input_transcription):
                        if text_chunk := response.server_content.input_transcription.text:
                            self.user_transcript_buffer += text_chunk
                            
                            if len(self.user_transcript_buffer.strip()) > 3:
                                await self.add_transcript(
                                    "user", 
                                    self.user_transcript_buffer.strip(), 
                                    is_final=False,
                                    input_type="audio"
                                )
                    
                    if (response.server_content and 
                        response.server_content.output_transcription):
                        if text_chunk := response.server_content.output_transcription.text:
                            if not self.interruption_event.is_set():
                                self.assistant_transcript_buffer += text_chunk
                                
                                if len(self.assistant_transcript_buffer.strip()) > 3:
                                    await self.add_transcript(
                                        "assistant", 
                                        self.assistant_transcript_buffer.strip(), 
                                        is_final=False,
                                        input_type="audio"
                                    )
                    
                    if text := response.text:
                        if not self.interruption_event.is_set():
                            if self.assistant_transcript_buffer:
                                await self.add_transcript(
                                    "assistant", 
                                    self.assistant_transcript_buffer.strip(), 
                                    is_final=True,
                                    input_type="audio"
                                )
                                self.assistant_transcript_buffer = ""
                            
                            await self.add_transcript("assistant", text, is_final=True, input_type="text")
                
                self.is_assistant_speaking = False
                
                if not self.interruption_event.is_set():
                    if self.user_transcript_buffer.strip():
                        await self.add_transcript(
                            "user", 
                            self.user_transcript_buffer.strip(), 
                            is_final=True,
                            input_type="audio"
                        )
                        self.user_transcript_buffer = ""
                    
                    if self.assistant_transcript_buffer.strip():
                        await self.add_transcript(
                            "assistant", 
                            self.assistant_transcript_buffer.strip(), 
                            is_final=True,
                            input_type="audio"
                        )
                        self.assistant_transcript_buffer = ""
                
            except Exception as e:
                await asyncio.sleep(0.01)
    
    async def run_session(self):
        try:
            if not await self.initialize_session():
                return
            
            async with genai_client.aio.live.connect(model=MODEL, config=self.config) as session:
                self.session = session
                self.is_active = True
                self.call_start_time = time.time()
                
                await self.session.send_realtime_input(text="This is your wake up message. initiate the conversation by saying 'This is your Meeting scheduler assitant how may i help you today?")
                
                tasks = [
                    self.send_realtime(),
                    self.receive_audio(),
                    self.process_text_input(),
                ]
                tasks.extend(self.get_bridge_specific_tasks())
                
                await asyncio.gather(*tasks)
            
        except asyncio.CancelledError:
            pass
        except Exception as e:
            pass
        finally:
            pass

    def get_bridge_specific_tasks(self):
        return []
    
    async def cleanup(self):
        self.is_active = False
        self.session = None
        
        self.call_end_time = time.time()
        self.call_duration_seconds = int(self.call_end_time - self.call_start_time)

        await self.print_final_transcript()

        if self.call_id:
            try:
                await save_call_history(
                    call_id=self.call_id,
                    transcripts=[t.dict() for t in self.transcripts if t.is_final],
                    call_summary=self.final_actionable_output.get("summary", ""),
                    actionable_items=self.final_actionable_output.get("actionable_items", [])
                )
            except Exception as e:
                pass

    async def print_final_transcript(self):
        json_transcript_output = []
        last_speaker_json = None
        buffered_text_json = ""

        for t in self.transcripts:
            if not t.is_final or not t.text.strip():
                continue

            current_speaker_json = "USER" if t.speaker == "user" else "Agent" if t.speaker == "assistant" else "SYSTEM"

            if last_speaker_json and current_speaker_json != last_speaker_json and last_speaker_json != "SYSTEM":          
                if buffered_text_json.strip():
                    json_transcript_output.append({
                        "speaker": last_speaker_json,
                        "text": buffered_text_json.strip(),
                        "timestamp": time.time(),
                        "input_type": getattr(t, 'input_type', 'unknown')
                    })
                buffered_text_json = ""

            buffered_text_json += " " + t.text.strip()
            last_speaker_json = current_speaker_json

        if buffered_text_json.strip() and last_speaker_json:
            json_transcript_output.append({
                "speaker": last_speaker_json,
                "text": buffered_text_json.strip(),
                "timestamp": time.time(),
                "input_type": "mixed"
            })

        self.final_json_transcript = json_transcript_output
        
        try:
            if not json_transcript_output or all(entry["text"].strip() == "" for entry in json_transcript_output):
                self.final_actionable_output = {"actionable_items": [], "summary": ""}
            else:
                full_transcript_text = ""
                for entry in json_transcript_output:
                    full_transcript_text += f'{entry["speaker"]}: {entry["text"]}\n'

                actionable_json_str = await asyncio.to_thread(actionable, full_transcript_text, self.llm_prompt_text)
                
                try:
                    actionable_response = json.loads(actionable_json_str)
                except json.JSONDecodeError as jde:
                    actionable_response = {}
                
                processed_items = await process_actions_from_actionable_response(actionable_response)
                self.final_actionable_output = {"actionable_items": processed_items, "summary": actionable_response.get("summary", "")}
            
        except Exception as e:
            pass

class WebCallBridge(GeminiBridgeBase):
    
    def __init__(self, agent_id: Optional[str] = None, call_id: Optional[str] = None):
        super().__init__("web", agent_id, call_id)
        self.web_sample_rate = 48000
        self.web_audio_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        
    async def process_web_audio(self):
        consecutive_speech_frames = 0
        speech_start_threshold = 2
        
        while self.is_active:
            try:
                web_audio_b64 = await asyncio.wait_for(
                    self.web_audio_queue.get(), timeout=0.01
                )
                
                pcm_data = self.audio_processor.convert_web_to_gemini_audio(
                    web_audio_b64, 
                    self.web_sample_rate
                )
                if not pcm_data:
                    continue
                
                speech_detected = await self.detect_speech_activity(pcm_data)
                
                if speech_detected:
                    consecutive_speech_frames += 1
                    
                    if consecutive_speech_frames >= speech_start_threshold:
                        self.user_speech_detected = True
                        self.last_user_audio_time = time.time()
                        self.silence_duration = 0
                        
                        if self.is_assistant_speaking:
                            await self.handle_interruption()
                else:
                    consecutive_speech_frames = 0
                    
                    current_time = time.time()
                    if self.user_speech_detected:
                        self.silence_duration = current_time - self.last_user_audio_time
                        
                        if self.silence_duration > self.silence_threshold:
                            self.user_speech_detected = False
                
                try:
                    await self.audio_out_queue.put({
                        "data": pcm_data,
                        "mime_type": "audio/pcm"
                    })
                    self.stats['audio_chunks_processed'] += 1
                except asyncio.QueueFull:
                    try:
                        await self.audio_out_queue.get()
                        await self.audio_out_queue.put({
                            "data": pcm_data,
                            "mime_type": "audio/pcm"
                        })
                    except:
                        pass
                    self.stats['queue_drops'] += 1
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                await asyncio.sleep(0.001)
    
    async def play_audio(self):
        while self.is_active:
            try:
                if self.interruption_event.is_set():
                    try:
                        while not self.audio_in_queue.empty():
                            self.audio_in_queue.get_nowait()
                    except:
                        pass
                    await asyncio.sleep(0.01)
                    continue
                
                bytestream = await asyncio.wait_for(self.audio_in_queue.get(), timeout=0.05)
                
                if self.interruption_event.is_set() or self.user_speech_detected:
                    continue
                
                async with self.playback_lock:
                    if not self.interruption_event.is_set():
                        audio_b64 = self.audio_processor.convert_gemini_to_web_audio(bytestream)
                        
                        if audio_b64 and self.websocket:
                            await self.websocket.send_text(json.dumps({
                                "type": "audio",
                                "audio": audio_b64
                            }))
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                await asyncio.sleep(0.001)
    
    async def handle_interruption(self):
        await super().handle_interruption()
        
        if self.websocket:
            try:
                await self.websocket.send_text(json.dumps({
                    "type": "clear_audio"
                }))
            except Exception as e:
                pass
    
    def get_bridge_specific_tasks(self):
        return [self.process_web_audio(), self.play_audio()]
    
    async def add_web_audio(self, audio_b64: str):
        try:
            await self.web_audio_queue.put(audio_b64)
        except asyncio.QueueFull:
            try:
                await self.web_audio_queue.get()
                await self.web_audio_queue.put(audio_b64)
            except:
                pass
    
    async def add_text_message(self, text: str):
        try:
            await self.text_input_queue.put(text)
        except asyncio.QueueFull:
            try:
                await self.text_input_queue.get()
                await self.text_input_queue.put(text)
            except:
                pass
    
    async def set_config(self, config: dict):
        if 'sampleRate' in config:
            self.web_sample_rate = config['sampleRate']
        
        if 'inputMode' in config:
            self.input_mode = config['inputMode']

active_sessions: Dict[str, WebCallBridge] = {}

async def register_session(bridge: WebCallBridge):
    active_sessions[bridge.session_id] = bridge

async def unregister_session(session_id: str):
    active_sessions.pop(session_id, None)

frontend_dist_path = "front/dist"
if os.path.exists(frontend_dist_path):
    app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dist_path, "assets")), name="assets")
    
    @app.get("/", response_class=FileResponse)
    async def serve_index():
        return FileResponse(os.path.join(frontend_dist_path, "index.html"))
else:
    app.mount("/static", StaticFiles(directory="static"), name="static")
    
    @app.get("/", response_class=FileResponse)
    async def serve_index():
        return FileResponse("static/index.html")

@app.websocket("/ws/web-call")
async def web_call_handler(websocket: WebSocket, agent_id: Optional[str] = Query(None)):
    await websocket.accept()
    
    current_bridge = WebCallBridge(agent_id=agent_id, call_id=str(uuid.uuid4()))
    current_bridge.websocket = websocket
    
    await register_session(current_bridge)
    
    try:
        session_task = asyncio.create_task(current_bridge.run_session())
        
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)
                await handle_web_message(data, current_bridge)
                
            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                pass
            except Exception as e:
                pass
    
    except Exception as e:
        pass
    finally:
        session_task.cancel()
        try:
            await session_task
        except asyncio.CancelledError:
            pass
        await current_bridge.cleanup()
        await unregister_session(current_bridge.session_id)

async def handle_web_message(data: Dict[str, Any], bridge_instance: WebCallBridge):
    msg_type = data.get('type')
    
    if msg_type == 'config':
        await bridge_instance.set_config(data)
        
    elif msg_type == 'audio':
        audio_data = data.get('audio', '')
        if audio_data:
            await bridge_instance.add_web_audio(audio_data)
    
    elif msg_type == 'text':
        text_data = data.get('text', '')
        if text_data.strip():
            await bridge_instance.add_text_message(text_data)
    
    elif msg_type == 'stop':
        await bridge_instance.cleanup()

async def save_call_history(
    call_id: str,
    transcripts: List[Dict[str, Any]],
    call_summary: str,
    actionable_items: List[Dict[str, Any]]
):
    global db_pool
    if not db_pool:
        return

    try:
        actionable_json = json.dumps(actionable_items, ensure_ascii=False) if actionable_items else json.dumps([])
        transcripts_json = json.dumps(transcripts, ensure_ascii=False) if transcripts else json.dumps([])

        query = """
            INSERT INTO call_history (call_id, transcripts, call_summary, actionable)
            VALUES ($1, $2::jsonb, $3, $4::jsonb);
        """
        await db_pool.execute(query, call_id, transcripts_json, call_summary, actionable_json)
    except Exception as e:
        pass

async def monitor_resources():
    while True:
        try:
            await asyncio.sleep(30)

        except Exception as e:
            await asyncio.sleep(30)

monitor_task: Optional[asyncio.Task] = None

@app.on_event("startup")
async def startup_event():
    global monitor_task, db_pool
    monitor_task = asyncio.create_task(monitor_resources())

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        pass
    else:
        try:
            global db_pool
            db_pool = await asyncpg.create_pool(db_url)
        except Exception as e:
            pass

@app.on_event("shutdown")
async def shutdown_event():
    global monitor_task, db_pool
    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    if db_pool:
        await db_pool.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', PORT))
    
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True
    )