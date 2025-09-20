# ROOMAN - AI Voice Assistant for Meeting Management

This is just a miniature version of my actual project where I have solely built enterprise-level project's backend (the whole Python part). It uses different telephony services for voice calls and also has a feature for web calls where users can just copy the provided link and paste it on their website, and our agent will act as their website's assistant. We developed tools which can scrape your site to create a knowledge base, and tools to write your prompts for your use case and industry. We integrated tools such as Gmail, Database, WhatsApp, Telegram, Slack, and SMS for different use cases. This solution can be used as pay-as-you-go for any single user (B2B) or for any organizations where they want to create their entire workforce.

Further capabilities from CallIntel - AI-Powered Calling (callintel.io):
• Designed and developed a scalable enterprise-grade conversational AI platform using Google Gemini and ElevenLabs as alternatives, automating real-time voice, messaging, and database interactions.
• Architected and deployed a modular microservices system with Python, FastAPI, Docker, Supabase, PostgreSQL, enabling independent scaling, reliability, and multi-tenant organization support.
• Engineered a real-time voice bridge with Twilio featuring µ-law ↔ PCM conversion, Voice Activity Detection (VAD), intelligent interruption handling, live transcription, summarization of calls, post-processing, and WebSocket-based streaming.
• Integrated model context protocols (MCPs) for WhatsApp (Selenium), Telegram (Telethon), Gmail (OAuth2), and PostgreSQL with AI-driven natural language to SQL querying for post-processing actions.
• Implemented advanced agent/session/organization management, including authentication, credential rotation, role-based permissions, and audit logging.
• Delivered enterprise features: batch calling, scheduled calls, outbound/inbound call APIs, real-time call management, email automation, WhatsApp/Telegram messaging, NLP-powered database queries.
• Ensured scalability, security, and reliability via Docker containerization, load balancing, encrypted credential storage, OAuth, multi-factor authentication, error recovery, and performance monitoring.
• Achieved sub-100ms real-time audio latency, concurrent session handling, rate-limited LLM interactions, and structured logging for system observability.

## Overview

ROOMAN is an intelligent AI voice assistant designed to help users manage their schedules and meetings efficiently through natural voice conversations. Built on Google's Gemini 2.5 Flash Live Preview model, it provides real-time voice interaction with advanced calendar management capabilities.

## What ROOMAN Does

ROOMAN serves as a **Meeting Assistant Pro** that helps users:
- **Schedule meetings** with natural language commands
- **Reschedule and update** existing calendar events
- **Cancel meetings** when needed
- **Query calendar information** about upcoming meetings
- **Send follow-up communications** via Gmail, WhatsApp, and Telegram
- **Process actionable items** from conversations automatically

The assistant maintains a conversational, friendly tone while being professional and helpful, acting as a personal meeting scheduler that understands context and can handle complex multi-step calendar operations.

## Key Features

### 🎤 **Real-Time Voice Interaction**
- **Live voice conversation** with Google Gemini 2.5 Flash Live Preview
- **Intelligent interruption handling** - users can interrupt the assistant mid-sentence
- **Voice Activity Detection (VAD)** with energy thresholds and zero-crossing rate analysis
- **Audio processing optimization** with minimal latency (48kHz to 16kHz conversion)
- **Dual input modes** - supports both voice and text input simultaneously

### 📅 **Advanced Calendar Management**
- **Google Calendar integration** via Model Context Protocol (MCP)
- **Natural language processing** for complex calendar queries
- **Multi-step operations** - can break down complex requests into actionable steps
- **Intelligent date/time parsing** with IST timezone support
- **CRUD operations** - Create, Read, Update, Delete meetings seamlessly

### 📧 **Communication Integration**
- **Gmail integration** for sending emails and checking connection status
- **WhatsApp and Telegram support** (infrastructure ready)
- **Automated follow-up** based on conversation analysis
- **Scheduled messaging** with precise timing control

### 🧠 **Intelligent Processing**
- **Conversation analysis** using Gemini 2.5 Flash for actionable item extraction
- **Post-processing pipeline** that identifies and executes follow-up actions
- **Context-aware responses** with conversation history maintenance
- **Error handling and retry mechanisms** with exponential backoff

### 🏗️ **Robust Architecture**
- **Microservices architecture** with separate MCP servers for Calendar and Gmail
- **WebSocket-based communication** for real-time interaction
- **PostgreSQL database** for call history and transcript storage
- **Docker containerization** for easy deployment and scaling
- **Resource monitoring** and session management

## Limitations

### 🚧 **Gemini Voice Model Limitations**
- **Tool calling optimization**: As Gemini voice models are still under development and in experimental phase, tool calling is not very optimized. During tool calling, the tool call execution should ideally run in the background while the agent continues to chat with the user, but this is not handled perfectly by Gemini, resulting in awkward silence before tool responses.

### 🔧 **Technical Limitations**
- **Single user authentication**: Currently configured for a single user (`sahillukhimultimedia_gmail_com`)
- **Limited communication channels**: WhatsApp and Telegram integrations are placeholder implementations
- **Timezone dependency**: Primarily optimized for IST (Indian Standard Time) timezone
- **Audio quality dependency**: Performance depends on microphone quality and network conditions
- **Concurrent session limits**: Limited by system resources and Gemini API rate limits

### 📱 **Platform Limitations**
- **Web browser dependency**: Requires modern browsers with WebRTC support
- **Network requirements**: Needs stable internet connection for real-time voice processing
- **Browser permissions**: Requires microphone access permissions

## Tools and APIs Used

### 🤖 **Core AI Engine**
- **Google Gemini 2.5 Flash Live Preview** - Primary voice AI model
- **Google Gemini 2.0 Flash** - For text processing and analysis
- **Google Generative AI SDK** - API integration

### 📅 **Calendar Integration**
- **Google Calendar API** - Calendar CRUD operations
- **Model Context Protocol (MCP)** - Tool integration framework
- **OAuth 2.0** - Google authentication
- **AsyncPG** - PostgreSQL database connectivity

### 📧 **Communication APIs**
- **Gmail API** - Email sending and management
- **MCP Gmail Server** - Custom Gmail integration layer
- **WhatsApp Business API** (placeholder)
- **Telegram Bot API** (placeholder)

### 🏗️ **Backend Infrastructure**
- **FastAPI** - Web framework and WebSocket handling
- **Uvicorn** - ASGI server
- **WebSockets** - Real-time communication
- **PostgreSQL** - Database for call history and transcripts
- **Docker & Docker Compose** - Containerization and orchestration

### 🎵 **Audio Processing**
- **NumPy** - Audio data manipulation
- **AudioOp** - Audio operations
- **WebRTC** - Browser audio capture
- **Base64 encoding** - Audio data transmission

### 📊 **Scheduling & Monitoring**
- **APScheduler** - Task scheduling for delayed actions
- **Loguru** - Advanced logging
- **Pytz** - Timezone handling
- **ThreadPoolExecutor** - Concurrent processing

## Setup Instructions

### Prerequisites
- Python 3.10+
- Docker and Docker Compose
- Google Cloud Platform account with Calendar and Gmail APIs enabled
- PostgreSQL database (included in Docker setup)

### 1. Environment Configuration

Create a `.env` file in the project root:
```bash
# Required API Keys
GEMINI_API_KEY=your_gemini_api_key_here
DATABASE_URL=postgresql://user:password@db:5432/mydatabase

# Server Configuration
PORT=8000
SERVER_DOMAIN=your_domain.com

# Optional: Frontend URL for CORS
FRONTEND_URL=http://localhost:5173
```

### 2. Google API Setup

#### Calendar API Setup:
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable Google Calendar API
4. Create OAuth 2.0 credentials
5. Download `credentials.json` and place in `mcp_calendar/` directory
6. Update redirect URIs to include `http://localhost:8102/oauth2callback`

#### Gmail API Setup:
1. Enable Gmail API in the same Google Cloud project
2. Create OAuth 2.0 credentials for Gmail
3. Download `credentials.json` and place in `mcp_gmail/` directory
4. Update redirect URIs to include `http://localhost:8101/oauth2callback`

### 3. Database Setup

The PostgreSQL database is automatically configured via Docker Compose. No manual setup required.

### 4. Installation and Deployment

#### Option A: Docker Compose (Recommended)
```bash
# Clone the repository
git clone <repository-url>
cd ROOMAN

# Build and start all services
docker-compose up --build

# The application will be available at:
# - Main app: http://localhost:8000
# - Calendar MCP: http://localhost:8102
# - Gmail MCP: http://localhost:8101
```

#### Option B: Local Development
```bash
# Install Python dependencies
pip install -r requirements.txt

# Start PostgreSQL (if not using Docker)
# Configure DATABASE_URL in .env

# Start MCP servers
python -m mcp_calendar.mcp_server &
python -m mcp_gmail.mcp_server &

# Start main application
python app.py
```

### 5. Initial Authentication

1. **Calendar Authentication**: Visit `http://localhost:8102` to authenticate with Google Calendar
2. **Gmail Authentication**: Visit `http://localhost:8101` to authenticate with Gmail
3. **Test the setup**: Open `http://localhost:8000` and start a voice conversation

### 6. Frontend Development (Optional)

If you want to modify the frontend:
```bash
cd front
npm install
npm run dev
```

## Usage Examples

### Voice Commands
- *"Schedule a team meeting for tomorrow at 10 AM"*
- *"What meetings do I have next week?"*
- *"Reschedule my 2 PM meeting to 3 PM"*
- *"Cancel all meetings on September 20th"*
- *"Email me the meeting notes to john@example.com"*

### Text Input
The system also supports text input for users who prefer typing over voice interaction.

## Architecture Overview

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Web Browser   │    │   FastAPI App    │    │  Gemini API    │
│   (Frontend)    │◄──►│   (app.py)       │◄──►│  (Voice AI)    │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                │
                                ▼
                       ┌─────────────────────┐
                       │   MCP Servers       │
                       │ ┌────────┐┌───────┐ │
                       │ │Calendar││Gmail│ │ │
                       │ │Server  ││Server │ │
                       │ └────────┘└───────┘ │
                       └─────────────────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │   PostgreSQL     │
                       │   Database       │
                       └──────────────────┘

```
