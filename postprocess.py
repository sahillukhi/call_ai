import os
import google.generativeai as genai
from dotenv import load_dotenv
import re
import json
import time
from mcp_gmail.mcp_client import gmail_client

import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

with open("prompt.txt", "r") as f:
    prompt = f.read()

def postprocess_prompt(input, agent_instruction=None):
    instruction_part = f"You are an AI assistant. {agent_instruction}" if agent_instruction else ""
    return f"""
    STRICT INSTRUCTION: {instruction_part} You are a specialized transcript analyzer. Follow these instructions EXACTLY as written.
    Do NOT provide explanations, analysis, or conversational responses about the transcript.
    Do NOT add any commentary or advice from your side.
    ONLY return the structured JSON response as specified below.
    
    TASK: Extract actionable items from AI-human call transcripts
    
    INPUT VALIDATION:
    - If transcript contains only greetings, wake-up calls, or minimal interaction without substantial conversation, return empty actionable_items and empty summary
    - If transcript appears incomplete or contains only agent prompts without customer response, return empty actionable_items and empty summary
    
    SCOPE: Extract ONLY these 3 types of actionable items:
    1. WhatsApp messages
    2. Telegram messages  
    3. Gmail/Email messages
    
    PREPROCESSING STEPS (Apply silently):
    1. Remove filler words (um, uh, like, you know)
    2. Identify speaker labels (AI/Human/Assistant/User/etc.)
    3. Correct obvious transcription errors
    4. Merge fragmented sentences
    
    Message in any of the Gmail, telegram, WhatsApp should be well detailed such that user should get the context from that
    
    ACTIONABLE TRIGGERS - Look for these exact phrases:
    - WhatsApp: "Send me on WhatsApp", "WhatsApp me", "send via WhatsApp", "message me on WhatsApp"
    - Telegram: "Send via Telegram", "Message me on Telegram", "send on Telegram", "Telegram me"
    - Gmail: "Email me", "Send to my Gmail", "Send via email", "send me an email"
    
    REQUIRED CONTACT INFORMATION:
    
    WhatsApp/Telegram:
    - Mobile number WITH country code (+1234567890 format)
    - Complete message content
    
    Gmail:
    - Valid email address (user@domain.com format)
    - Email subject line
    - Email body content
    
    There should always be a field of action_list where if multiple actions are there add multiple actions there in single list

    VALIDATION RULES (MANDATORY):
    - Phone numbers MUST have country code (+XX format) or mark as "needs_clarification"
    - Email addresses MUST be valid format or mark as "needs_clarification"
    - ALL required fields MUST be present or mark as "needs_clarification"
    - If actionable items are outside scope (not WhatsApp/Telegram/Gmail), return empty list
    - If transcript contains only greetings or wake-up calls, return empty actionable_items and empty summary
    - If `scheduled_time` is provided, it MUST be a valid ISO 8601 UTC timestamp or mark as "needs_clarification". If `scheduled_time` is provided, the `status` will always be `scheduled` otherwise it will be `pending`

    RESPONSE FORMAT (MANDATORY - Return ONLY this JSON structure):
    {{
        "summary": "detailed summary of the transcript which can be used to understand whole conversation instead of reading the trancsript",
        "action_list": ["whatsapp", "telegram", "gmail"],
        "actionable_items": [
            {{
                "id": 1,
                "key": "whatsapp",
                "data": {{
                    "mobile_number": "+1234567890",
                    "message": "Complete message content",
                    "status": "pending",
                    "notes": "",
                    "scheduled_time": "YYYY-MM-DDTHH:MM:SSZ"
                }}
            }},
            {{
                "id": 2,
                "key": "telegram",
                "data": {{
                    "mobile_number": "+1234567890", 
                    "message": "Complete message content",
                    "status": "pending",
                    "notes": "",
                    "scheduled_time": "YYYY-MM-DDTHH:MM:SSZ"
                }}
            }},
            {{
                "id": 3,
                "key": "gmail",
                "data": {{
                    "email_address": "user@example.com",
                    "subject": "Email subject",
                    "body": "Email body content",
                    "status": "pending",
                    "notes": "",
                    "scheduled_time": "YYYY-MM-DDTHH:MM:SSZ"
                }}
            }}
        ],
        "confidence_score": 0.95
    }}

    STATUS VALUES:
    - "pending": All required information is available and action is to be sent instantly
    - "scheduled": All required information is available and action is to be scheduled
    - "needs_clarification": Missing required information
    
    CRITICAL RULES:
    1. Each actionable item has ONLY ONE key (whatsapp OR telegram OR gmail) but from single transcript there could be multiple requests such as one information on telegram, another on gmail
    2. If no actionable items found, return empty actionable_items array
    3. Do NOT include any text outside the JSON response
    4. Do NOT provide explanations or analysis
    5. Do NOT add conversational responses
    6. If transcript is unclear, unrelated, or contains only minimal interaction (greetings, wake-up calls), return empty summary and empty actionable_items
    7. If `scheduled_time` is provided, ensure it is a valid ISO 8601 UTC timestamp, and set the status to "scheduled". Otherwise, if no `scheduled_time` is provided, set status to "pending".
    
    EXAMPLES OF CORRECT RESPONSES:
    
    Example 1 (No actionable items - minimal interaction):
    {{
        "summary": "",
        "action_list": [],
        "actionable_items": [],
        "confidence_score": 1.0
    }}
    
    Example 2 (Wake-up call only):
    {{
        "summary": "",
        "action_list": [],
        "actionable_items": [],
        "confidence_score": 1.0
    }}
    
    Example 3 (WhatsApp request - instant):
    {{
        "summary": "detailed summary of the transcript which can be used to understand whole conversation instead of reading the trancsript",
        "action_list": ["whatsapp"],
        "actionable_items": [
            {{
                "id": 1,
                "key": "whatsapp",
                "data": {{
                    "mobile_number": "+1234567890",
                    "message": "Monthly sales report as requested",
                    "status": "pending",
                    "notes": "",
                    "scheduled_time": null
                }}
            }}
        ],
        "confidence_score": 0.90
    }}

    Example 4 (WhatsApp and telegram request - scheduled and instant):
    {{
        "summary": "detailed summary of the transcript which can be used to understand whole conversation instead of reading the trancsript",
        "action_list": ["whatsapp", "telegram"],
        "actionable_items": [
            {{
                "id": 1,
                "key": "whatsapp",
                "data": {{
                    "mobile_number": "+1234567890",
                    "message": "Monthly sales report as requested",
                    "status": "scheduled",
                    "notes": "",
                    "scheduled_time": "2025-09-19T10:00:00Z"
                }}
            }},
            {{
                "id": 2,
                "key": "telegram",
                "data": {{
                    "mobile_number": "+1234567890",
                    "message": "Monthly sales report as requested",
                    "status": "pending",
                    "notes": "",
                    "scheduled_time": null
                }}
            }}
        ],
        "confidence_score": 0.90
    }}
    
    Example 5 (Missing information):
    {{
        "summary": "detailed summary of the transcript which can be used to understand whole conversation instead of reading the trancsript",
        "action_list": ["gmail"],
        "actionable_items": [
            {{
                "id": 1,
                "key": "gmail",
                "data": {{
                    "email_address": "",
                    "subject": "Requested document",
                    "body": "Document as requested",
                    "status": "needs_clarification",
                    "notes": "Email address not provided",
                    "scheduled_time": null
                }}
            }}
        ],
        "confidence_score": 0.60
    }}
    
    FINAL INSTRUCTION: 
    Analyze the following transcript and return ONLY the JSON response. 
    Do not include any other text, explanations, or commentary.
    If the transcript contains only greetings, wake-up calls, or minimal interaction, return empty summary and empty actionable_items.
    
    TRANSCRIPT TO ANALYZE:
    {input}
    
    Send detailed response to the user and not a single line response such as here is the information add actucal data there

    and here is the Datasource to send user proper response {prompt}
    """

def actionable(input_text: str, llm_prompt_text: str = None) -> str:
    model = genai.GenerativeModel(model_name="gemini-2.5-flash")
    try:
        response = model.generate_content(postprocess_prompt(input_text, agent_instruction=llm_prompt_text))
        raw_text = response.text.strip()
        cleaned_text = re.sub(r"^```(?:json)?\s*([\s\S]*?)\s*```$", r"\1", raw_text)
        return cleaned_text
    except Exception as e:
        return f"[ERROR] Gemini generation failed: {str(e)}"
    

async def telegram(data):
    pass

def whatsapp(data):
    pass

async def gmail(data):
    await gmail_client(data)

async def process_actions_from_actionable_response(response_json, agent_id=None, max_retries=3, scheduler: AsyncIOScheduler = None):
    if not response_json or "action_list" not in response_json or not isinstance(response_json["action_list"], list) or not response_json["action_list"]:
        return []
    if "actionable_items" not in response_json or not isinstance(response_json["actionable_items"], list):
        return []

    updated_actionable_items = []
    for idx, item in enumerate(response_json["actionable_items"]):
        key = item.get("key")
        data = item.get("data")
        
        if not key or not data or not isinstance(data, dict):
            item["data"] = item.get("data", {})
            item["data"]["status"] = "failed"
            item["data"]["notes"] = "Malformed item or missing key/data"
            updated_actionable_items.append(item)
            continue
        
        action_func = None
        if key == "telegram":
            action_func = telegram
        elif key == "whatsapp":
            action_func = whatsapp
        elif key == "gmail":
            action_func = gmail
        else:
            item["data"]["status"] = "failed"
            item["data"]["notes"] = f"Unknown action key: {key}"
            updated_actionable_items.append(item)
            continue
        
        attempt = 0
        while attempt < max_retries:
            try:
                scheduled_time_str = data.get("scheduled_time")

                if scheduled_time_str:
                    try:
                        scheduled_time = datetime.fromisoformat(scheduled_time_str.replace("Z", "+00:00"))
                        scheduler.add_job(action_func, 'date', run_date=scheduled_time, args=[data])
                        item["data"]["status"] = "scheduled"
                        item["data"]["notes"] = f"Action scheduled for {scheduled_time}"
                    except ValueError as ve:
                        item["data"]["status"] = "needs_clarification"
                        item["data"]["notes"] = f"Invalid scheduled_time format: {ve}"
                    except Exception as e:
                        item["data"]["status"] = "failed"
                        item["data"]["notes"] = f"Failed to schedule: {e}"
                else:
                    await action_func(data)
                    item["data"]["status"] = "completed"
                break
            except Exception as e:
                item["data"]["status"] = "failed"
                item["data"]["notes"] = str(e)
                attempt += 1
                if attempt < max_retries:
                    sleep_time = 2 ** attempt
                    await asyncio.sleep(sleep_time)
                else:
                    pass
        updated_actionable_items.append(item)
    return updated_actionable_items