import asyncio
import json
from typing import Optional
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

def llm_client(message: str) -> str:
    try:
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel(model_name="gemini-2.0-flash")
        response = model.generate_content(message)
        text = response.text.strip()

        text = re.sub(r"^```(?:json)?\s*([\s\S]*?)\s*```$", r"\1", text.strip())

        return text
    except Exception as e:
        return f"[ERROR] Gemini generation failed: {str(e)}"

def get_prompt_for_tool_selection(query, tools, user_id):
    if not tools or not hasattr(tools, 'tools') or not tools.tools:
        return f"No tools available. Please respond directly to: {query}"
    
    tools_description = "\n".join([
        f"- {tool.name}: {tool.description}\n  Input schema: {getattr(tool, 'inputSchema', 'No schema available')}" 
        for tool in tools.tools
    ])
    
    return f"""You are a helpful assistant with access to email tools.

Available tools:
{tools_description}

User Request: {query}

Choose the appropriate action:

1. For sending emails, respond with JSON:
{{
    "tool": "send_email",
    "arguments": {{
        "user_id": "{user_id}",
        "to": "recipient@example.com",
        "subject": "Email subject",
        "body": "Email content"
    }}
}}

2. For checking connection status, respond with JSON:
{{
    "tool": "check_connection", 
    "arguments": {{
        "user_id": "{user_id}"
    }}
}}

3. For other requests, respond with JSON:
{{
    "direct_response": "I can help with sending emails and checking connection status. Please authenticate first at http://localhost:8101"
}}

Ensure all values are strings and match parameter names exactly."""

async def gmail_client(query: str, user_id: Optional[str] = None):
    sse_url = "http://localhost:8101/sse"

    try:
        if user_id is None:
            user_id = get_user_id_from_token_file()
            if user_id is None:
                return "Error: No authenticated user found. Please authenticate at http://localhost:8101"

        async with sse_client(url=sse_url) as (in_stream, out_stream):
            async with ClientSession(in_stream, out_stream) as session:
                info = await session.initialize()
                
                tools = await session.list_tools()
                
                prompt = get_prompt_for_tool_selection(query, tools, user_id)
                response = llm_client(prompt)
                
                try:
                    tool_call = json.loads(response)
                    
                    if "direct_response" in tool_call:
                        return tool_call["direct_response"]
                    
                    tool_name = tool_call["tool"]
                    arguments = tool_call["arguments"]

                    if "user_id" in arguments:
                        arguments["user_id"] = user_id
                    else:
                        if tool_name in ["send_email", "get_user_emails", "check_connection"]:
                            arguments["user_id"] = user_id

                    result = await session.call_tool(
                        tool_name, arguments=arguments
                    )

                    if result.content:
                        try:
                            content_text = result.content[0].text if result.content[0].text else "{}"
                            parsed_content = json.loads(content_text)
                            
                            if tool_name == "send_email":
                                if parsed_content.get("status") == "Email sent successfully":
                                    response_text = (f"Email sent successfully!\n"
                                                   f"Message ID: {parsed_content.get('messageId')}\n"
                                                   f"Thread ID: {parsed_content.get('threadId')}")
                                else:
                                    status = parsed_content.get('status', 'Unknown error')
                                    if "not authenticated" in status.lower() or "credentials expired" in status.lower():
                                        response_text = f"Authentication required: {status}\n\nPlease visit http://localhost:8101 to authenticate with Google."
                                    else:
                                        response_text = f"Email sending failed: {status}"
                            elif tool_name == "get_user_emails":
                                if parsed_content.get("status") == "Success" and parsed_content.get("emails"):
                                    emails = parsed_content["emails"]
                                    response_text = "Recent Emails:\n"
                                    for i, email_data in enumerate(emails):
                                        response_text += (f"  {i+1}. From: {email_data.get('from', 'N/A')}\n"
                                                          f"     Subject: {email_data.get('subject', 'N/A')}\n"
                                                          f"     Date: {email_data.get('date', 'N/A')}\n"
                                                          f"     Snippet: {email_data.get('snippet', 'N/A')}\n")
                                else:
                                    response_text = f"Failed to get emails: {parsed_content.get('status', 'Unknown error')}"
                            elif tool_name == "check_connection":
                                response_text = f"Connection Status: {parsed_content.get('status', 'Unknown')}"
                            else:
                                response_text = json.dumps(parsed_content, indent=2)
                                
                        except json.JSONDecodeError:
                            response_text = "".join([content.text if hasattr(content, 'text') else str(content) for content in result.content])
                        
                        return response_text
                    else:
                        return "Tool executed successfully but returned no content."
                        
                except json.JSONDecodeError as e:
                    return f"Error: Failed to parse tool selection - {response}"
                except Exception as e:
                    return f"Error calling tool: {str(e)}"
                    
    except Exception as e:
        return f"Connection error: {str(e)}. Please ensure the Gmail MCP server is running at http://localhost:8101"

def get_user_id_from_token_file() -> Optional[str]:
    token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tokens')
    if not os.path.exists(token_path):
        return None

    token_files = [f for f in os.listdir(token_path) if f.startswith('token_') and f.endswith('.json')]

    if not token_files:
        return None
    
    first_token_file = token_files[0]
    user_id = first_token_file.replace('token_', '').replace('.json', '')
    return user_id

if __name__ == "__main__":
    example_query = """{
      "id": 2,
      "key": "gmail",
      "data": {
        "email_address": "sahillukhi9@gmail.com",
        "subject": "Information about GradientCurve - Digital Solutions for Your Business",
        "body": "Hi there,\n\nHere's comprehensive information about GradientCurve, your partner for innovative digital solutions:\n\n**Company Overview:**\n*   **Full Name:** GradientCurve — a dynamic technology team delivering comprehensive end-to-end digital solutions.\n*   **Founded:** 2025\n*   **Headquarters:** Surat, Gujarat\n*   **Founders:** Sahil B. Lukhi, Parth Mangnani\n*   **Team Size:** 12 skilled professionals\n*   **Mission:** To design and deliver innovative, high-performance, and scalable digital solutions that empower businesses to thrive in the AI-driven era.\n\n**Core Offerings (Our Solutions & Services):**\n*   **AI/ML & Generative AI:** From advanced LLMs (RAG, LLaMA 3, GPT-4, Gemini 1.5) and AI Agent Systems (CrewAI, LangChain) to Computer Vision (YOLO, OpenCV), Deep Learning, and MLOps.\n*   **Full-Stack Development:** Robust backend (Python, Node.js, PHP, Java) and dynamic frontend (ReactJS, NextJS) development with various database integrations.\n*   **UI/UX Design:** User-centric design focusing on responsive design, wireframing, and prototyping using tools like Figma and Adobe XD.\n*   **DevOps & Cloud:** Expertise in platforms like AWS, GCP, Docker, Kubernetes, and Git for seamless deployment and infrastructure management.\n*   **Data Science & Analytics:** Comprehensive data analysis, statistical forecasting, and visualization using Power BI, Plotly, and more.\n*   **Automation & Workflow Solutions:** Implementing workflow automation platforms (n8n) and custom AI agents to streamline business processes.\n\n**Notable Projects (Highlights):**\n*   **AI/ML:** Intelligent AI Agent Systems (e.g., Automated proposal writing, Travel Planner AI Agent), RAG & Knowledge Graph Systems (AI-Powered Information Retrieval), Computer Vision (Real-time fall detection, surface anomaly classification), Predictive Analytics (Hotel Bookings Forecast), Image to Audio Story Generator.\n*   **Web & Mobile:** E-commerce platforms (CromiTopia, Shopify customizations), Social Platforms, and Specialized Applications (Fitness trackers, parking management, laundry apps like WashOn).\n*   **Data Analytics:** T20 World Cup analytics dashboards, Business Intelligence solutions.\n*   **Automation & Workflow:** Custom automation pipelines reducing manual tasks by 60%.\n\n**Our Differentiators:**\n1.  **End-to-End Solutions:** Complete project lifecycle from concept to deployment.\n2.  **Cross-Domain Expertise:** Experience across e-commerce, healthtech, fintech, industrial AI.\n3.  **Results-Driven:** Focused on measurable business outcomes.\n4.  **Scalable Architecture:** Cloud-native, production-ready systems.\n5.  **User-Centric Approach:** Strong focus on UI/UX and product usability.\n\n**Contact Information:**\n*   **Primary Contact:** Sahil B. Lukhi\n*   **Email:** sahillukhi9@gmail.com\n*   **Phone:** +91 78745 27239\n\nFeel free to reach out if you have any further questions or would like to discuss a potential project!\n\nBest regards,\nSahil B. Lukhi\nGradientCurve",
        "status": "completed",
        "notes": ""
      }
    }"""
    
    print(f"\n--- Running Simple Gmail Client ---")
    print(f"Query: {example_query}")
    result = asyncio.run(gmail_client(example_query))
    print(f"Result: {result}\n")