import asyncio
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import pytz
from mcp import ClientSession
from mcp.client.sse import sse_client
import google.generativeai as genai
import httpx
from loguru import logger
import os
import re
from dotenv import load_dotenv

load_dotenv()

_orig_request = httpx.AsyncClient.request

async def _patched_request(self, method, url, *args, **kwargs):
    kwargs.setdefault("follow_redirects", True)
    return await _orig_request(self, method, url, *args, **kwargs)

httpx.AsyncClient.request = _patched_request

USER_ID = "sahillukhimultimedia_gmail_com"

def get_ist_and_utc():
    utc = datetime.now(pytz.utc)
    ist = utc.astimezone(pytz.timezone("Asia/Kolkata"))
    return {
        "UTC": utc.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "IST": ist.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "utc_datetime": utc,
        "ist_datetime": ist
    }

def llm_client(prompt: str, history: Optional[List[Dict[str, str]]] = None, api_key: Optional[str] = None) -> str:
    if history is None:
        history = []
    try:
        if api_key is None:
            api_key = os.getenv("GEMINI_API_KEY")
        
        if not api_key:
            return "[ERROR] GEMINI_API_KEY not configured."
            
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name="gemini-2.0-flash")
        
        chat = model.start_chat(history=history)
        response = chat.send_message(prompt)
        text = response.text.strip()

        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if json_match:
            text = json_match.group(1).strip()
        else:
            text = text.strip()
            if text.startswith("I am following your instructions to delete meeting with id "):
                text = re.sub(r"^I am following your instructions to delete meeting with id '.*?'\s*", "", text).strip()
            start_idx = text.find("{")
            end_idx = text.rfind("}")
            if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
                text = text[start_idx : end_idx + 1]

        return text
    except Exception as e:
        return f"[ERROR] Gemini generation failed: {str(e)}"

def get_prompt_for_calendar_tool_selection(query: str, tools, user_id: str) -> str:
    if not tools or not hasattr(tools, 'tools') or not tools.tools:
        return f"No tools available. Please respond directly to: {query}"
    
    time_context = get_ist_and_utc()
    current_ist = time_context["ist_datetime"]
    
    tools_description = "\n".join([
        f"- {tool.name}: {tool.description}\n  Input schema: {getattr(tool, 'inputSchema', 'No schema available')}" 
        for tool in tools.tools
    ])
    
    return f"""You are an intelligent calendar assistant with access to calendar management tools.

CURRENT TIME CONTEXT:
- Current IST Time: {time_context['IST']}
- Current UTC Time: {time_context['UTC']}
- Today's Date: {current_ist.strftime('%Y-%m-%d')}
- Current Day: {current_ist.strftime('%A')}

Available Calendar Tools:
{tools_description}

User Request: "{query}"

INSTRUCTIONS:
1. Analyze the user's intent and extract all relevant information (dates, times, event details, etc.)
2. When dealing with dates and times:
   - Convert all relative time references (e.g., "today", "tomorrow", "next week") to absolute dates in YYYY-MM-DD format.
   - Always use IST timezone ("+05:30") for all times unless explicitly specified otherwise by the user.
   - Ensure times are in ISO format: "YYYY-MM-DDTHH:MM:SS+05:30".
   - Be intelligent about interpreting ambiguous date/time requests.

3. Based on the user's request, determine the most appropriate action(s). Your response should be a JSON object with two main keys: `action_plan` and `execution_plan`.

4. `action_plan`: Provide a brief, high-level summary of the overall strategy to fulfill the user's request. This helps in understanding your multi-step reasoning.

5. `execution_plan`: This will be a *list* of immediate actions to take. Each item in the list can be either a `tool` call or a `direct_response`. The system will execute these steps sequentially.
   - If a tool is needed, structure it as:
     {{
         "tool": "tool_name",
         "arguments": {{ /* key-value pairs for tool arguments */ }}
     }}
   - If the task is completed or no further tool action is needed *after executing previous steps in the plan*, structure the final step as:
     {{
         "direct_response": "Your helpful response here"
     }}

IMPORTANT:

- Always include the user_id: "{user_id}" in tool arguments.
- For complex tasks, generate a sequence of tool calls in `execution_plan` to complete the entire task in one LLM turn if possible (e.g., read meetings, then delete multiple). The system will execute them one by one.
- Only provide a `direct_response` as the *last* item in `execution_plan` when the entire user request has been fulfilled.
- If you need to gather more information before making a definitive plan, use a `read_meetings` tool call as the first step, and the system will re-evaluate with the new information.
- Do not make anything static and maintain a conversation history for each session up to a point where the task is completed.

Example for creating a meeting (single step):
{{
    "action_plan": "Create a new meeting with the provided details.",
    "execution_plan": [
        {{
            "tool": "create_meeting",
            "arguments": {{
                "user_id": "{user_id}",
                "summary": "Team Standup",
                "description": "Daily sync-up",
                "start_time": "2025-09-22T09:00:00+05:30",
                "end_time": "2025-09-22T09:30:00+05:30",
                "location": "Virtual"
            }}
        }},
        {{
            "direct_response": "Team Standup created for tomorrow at 9 AM."
        }}
    ]
}}

Example for cancelling all meetings on a specific date (multi-step):
{{
    "action_plan": "First, retrieve all meetings for the specified date. Then, iterate through the found meetings and delete each one.",
    "execution_plan": [
        {{
            "tool": "read_meetings",
            "arguments": {{
                "user_id": "{user_id}",
                "time_min": "2025-09-20T00:00:00+05:30",
                "time_max": "2025-09-20T23:59:59+05:30"
            }}
        }},
        {{ "instruction": "The system will now process the results from read_meetings and dynamically generate delete_meeting calls for each meeting found. A final direct_response will be provided upon completion." }}
    ]
}}

Example for direct response (task completed or no tool needed initially):
{{
    "action_plan": "The user is asking a question that does not require a tool call.",
    "execution_plan": [
        {{
            "direct_response": "I am an intelligent calendar assistant, ready to help you manage your schedule. How can I assist you today?"
        }}
    ]
}}"""

async def calendar_client(query: str, user_id: Optional[str] = None):
    sse_url = "http://localhost:8102/sse"

    try:
        if user_id is None:
            user_id = USER_ID

        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            return "Error: GEMINI_API_KEY is not set. Please configure your .env file."

        async with sse_client(url=sse_url) as (in_stream, out_stream):
            async with ClientSession(in_stream, out_stream) as session:
                info = await session.initialize()
                
                tools = await session.list_tools()

                conversation_history = []
                original_query = query
                tool_output_context = ""
                overall_actions_summary = []

                while True:
                    prompt_for_first_llm = get_prompt_for_calendar_tool_selection(query, tools, user_id)
                    
                    if tool_output_context:
                        prompt_for_first_llm += f"\n\nPrevious Tool Output (for analysis): {tool_output_context}"

                    conversation_history.append({"role": "user", "parts": [prompt_for_first_llm]})

                    llm_response_1 = llm_client(prompt_for_first_llm, conversation_history, api_key=gemini_api_key)
                    conversation_history.append({"role": "model", "parts": [llm_response_1]})
                    
                    try:
                        parsed_llm_response_1 = json.loads(llm_response_1)
                        action_plan = parsed_llm_response_1.get("action_plan", "No action plan provided.")
                        execution_plan = parsed_llm_response_1.get("execution_plan", [])

                        if execution_plan:
                            tool_output_context = ""
                            current_plan_processed = True
                            for step in execution_plan:
                                if "tool" in step:
                                    tool_name = step["tool"]
                                    arguments = step["arguments"]
                                    tool_result_raw = await execute_single_tool_call(session, step, user_id)

                                    tool_output_context = tool_result_raw

                                    try:
                                        parsed_tool_result = json.loads(tool_result_raw)
                                        formatted_result = format_calendar_response(tool_name, parsed_tool_result)
                                        overall_actions_summary.append(formatted_result)
                                    except json.JSONDecodeError:
                                        overall_actions_summary.append(f"Tool '{tool_name}' executed. Raw Result: {tool_result_raw}")
                                    
                                    if tool_name == "read_meetings":
                                        try:
                                            read_meetings_response = json.loads(tool_result_raw)
                                            if read_meetings_response.get("success") and read_meetings_response.get("meetings"):
                                                current_plan_processed = False
                                                break
                                        except json.JSONDecodeError:
                                            pass

                                elif "direct_response" in step:
                                    overall_actions_summary.append(step["direct_response"])
                                    return "\n".join(overall_actions_summary)
                                
                                elif "instruction" in step:
                                    overall_actions_summary.append(f"Instruction: {step['instruction']}")
                                    if "dynamically generate" in step["instruction"] and tool_output_context:
                                        current_plan_processed = False
                                        break

                            if current_plan_processed:
                                return "\n".join(overall_actions_summary)
                            else:
                                pass

                        else:
                            return f"Error: Invalid LLM response format - 'execution_plan' is empty or not found: {llm_response_1}"

                    except json.JSONDecodeError as e:
                        return f"Error: Failed to parse tool selection - {llm_response_1}"
                    except Exception as e:
                        return f"Error in calendar operation: {str(e)}"
                    
    except Exception as e:
        return f"Connection error: {str(e)}. Please ensure the Calendar MCP server is running at http://localhost:8102"

async def execute_single_tool_call(session, tool_call: Dict[str, Any], user_id: str) -> str:
    tool_name = tool_call["tool"]
    arguments = tool_call["arguments"]

    arguments["user_id"] = user_id

    result = await session.call_tool(tool_name, arguments=arguments)

    if result.content:
        try:
            content_text = result.content[0].text if result.content[0].text else "{}"
            parsed_content = json.loads(content_text)
            
            return json.dumps(parsed_content)
            
        except json.JSONDecodeError:
            response_text = "".join([
                content.text if hasattr(content, 'text') else str(content) 
                for content in result.content
            ])
            return response_text
    else:
        return json.dumps({"success": False, "error": "Tool executed successfully but returned no content."})

def format_calendar_response(tool_name: str, parsed_content: Dict[str, Any], step_context: str = "") -> str:
    
    if tool_name == "create_meeting":
        if parsed_content.get("success"):
            return f"Meeting created successfully! Meeting ID: {parsed_content.get('meeting', {}).get('id', 'N/A')} Time: {parsed_content.get('meeting', {}).get('start', {}).get('dateTime', 'N/A')} - {parsed_content.get('meeting', {}).get('end', {}).get('dateTime', 'N/A')}"
        else:
            return f"Failed to create meeting: {parsed_content.get('error', 'Unknown error')}"
    
    elif tool_name == "read_meetings":
        if parsed_content.get("success") and parsed_content.get("meetings"):
            meetings = parsed_content["meetings"]
            if not meetings:
                return "No meetings found for the specified time period."
            
            response = f"Found {len(meetings)} meeting(s):\n"
            for i, meeting in enumerate(meetings, 1):
                response += f"  {i}. {meeting.get('title', 'Untitled')}\n"
                start_time = meeting.get('start', {}).get('dateTime', 'N/A')
                end_time = meeting.get('end', {}).get('dateTime', 'N/A')
                response += f"     {start_time} - {end_time}\n"
                if meeting.get('description'):
                    response += f"     {meeting.get('description')}\n"
                response += f"     ID: {meeting.get('id', 'N/A')}\n"
            return response
        else:
            return f"Failed to get meetings: {parsed_content.get('error', 'Unknown error')}"
    
    elif tool_name == "update_meeting":
        if parsed_content.get("success"):
            meeting_data = parsed_content.get('meeting', {})
            return f"Meeting updated successfully! Meeting ID: {meeting_data.get('id', 'N/A')} New Time: {meeting_data.get('start', {}).get('dateTime', 'N/A')} - {meeting_data.get('end', {}).get('dateTime', 'N/A')}"
        else:
            return f"Failed to update meeting: {parsed_content.get('error', 'Unknown error')}"
    
    elif tool_name == "delete_meeting":
        if parsed_content.get("success"):
            return f"Meeting deleted successfully! Meeting ID: {parsed_content.get('meeting_id', 'N/A')}"
        else:
            return f"Failed to delete meeting: {parsed_content.get('error', 'Unknown error')}"
    
    elif tool_name == "check_meeting_auth":
        if parsed_content.get("success"):
            return f"Authentication status: {parsed_content.get('message', 'Connected')}"
        else:
            return f"Authentication issue: {parsed_content.get('error', 'Unknown error')}"
    
    else:
        return json.dumps(parsed_content, indent=2)

if __name__ == "__main__":
    test_queries = [
        "cancle all the meetings held on 20-09"
    ]
    
    for query in test_queries:
        print(f"\n--- Testing Query: {query} ---")
        result = asyncio.run(calendar_client(query))
        print(f"Result: {result}\n")