"""
codex-chat-app/app.py

Main application for the codex Chat App — a self-hosted codex chat interface.

What it does:
  - Provides a multi-turn streaming chat UI powered by codex models on Amazon
    Bedrock Mantle, accessed through the LiteLLM proxy using the OpenAI SDK's
    responses.create() method with the user's personal API key.
  - Web search: when enabled, codex is given a web_search tool definition.
    On the first non-streaming call, if codex decides to invoke the tool, the
    app calls DuckDuckGo (Instant Answer API, with an HTML scraping fallback),
    injects the results as a tool message, then makes a second streaming call
    to produce the final answer with real-time grounding.
  - File upload: accepts PDFs, DOCX, XLSX, CSV, code files, config files,
    images, and plain text. Files are processed in memory by file_handler.py
    and prepended as text or base64 image blocks in the user message.
  - Sidebar features:
      - Dynamic model selector — fetches models allowed for the user's API key.
      - Budget display — shows spend progress bar, remaining USD, TPM/RPM limits.
      - Web search toggle — disables tool-use for offline/private sessions.
      - Recent chat list — last 20 sessions stored in session state + localStorage.
  - Session management: new chat button, chat switching, and auto-save.

Architecture note:
  LiteLLM config uses bedrock_mantle/ provider prefix which routes directly to
  the Bedrock Mantle OpenAI-compatible API. This avoids the LiteLLM v1.89.x bug
  where the internal responses-to-completion bridging causes functools.partial()
  recursion. The proxy handles per-user budgets, model permissions, and logging.
  EC2 IAM role is used for Bedrock authentication — no api_key needed in config.

Environment variables consumed:
  LITELLM_BASE_URL   — proxy address (default: http://litellm-proxy:4000).
  DEFAULT_MODEL      — fallback model if none is selected (default: gpt-5.5).
  SEARCH_PROVIDER    — web search backend (currently "duckduckgo").
  LITELLM_MASTER_KEY — used by ChatConfig for admin endpoints.

Depends on:
  auth.py                        — check_password() / logout_button().
  utils/file_handler.py          — prepare_file_for_api() for all attachment types.
  utils/chat_config_loader.py    — ChatConfig for model list and budget info.
"""

import streamlit as st
import os
import json
import re
import base64
import time
import requests as req_lib
from datetime import datetime
from openai import OpenAI


# ===== PAGE CONFIG (MUST BE FIRST STREAMLIT COMMAND) =====
st.set_page_config(
    page_title="codex Chat",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ===== AUTHENTICATION =====
from auth import check_password, logout_button

if not check_password():
    st.stop()


# ===== CONFIGURATION =====
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://litellm-proxy:4000")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-5.5")
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "duckduckgo")
WEB_SEARCH_ENABLED = True


# ===== IMPORTS FROM UTILS =====
from utils.file_handler import prepare_file_for_api
from utils.chat_config_loader import (
    get_chat_config,
    get_display_name,
    get_model_alias,
)


# ===== OPENAI CLIENT (points to LiteLLM proxy) =====
def get_openai_client():
    """
    Get OpenAI client configured to use LiteLLM proxy.
    The user's virtual key authenticates them to LiteLLM.
    LiteLLM then uses EC2 IAM role to call Bedrock Mantle.
    """
    api_key = st.session_state.get("api_key") or "sk-placeholder"
    return OpenAI(
        api_key=api_key,
        base_url=LITELLM_BASE_URL,
    )


# ===== WEB SEARCH TOOL DEFINITION =====
WEB_SEARCH_TOOL = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the web for current information. Use this when the user "
        "asks about recent events, current data, latest versions, live "
        "information, or anything that may have changed after your training data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web"
            }
        },
        "required": ["query"]
    }
}


# ===== WEB SEARCH FUNCTIONS =====
def perform_web_search(query):
    """Perform web search using DuckDuckGo (free, no API key needed)."""
    try:
        return _search_duckduckgo(query)
    except Exception as e:
        return "Search failed: " + str(e)


def _search_duckduckgo(query):
    """Free web search using DuckDuckGo Instant Answer API."""
    try:
        params = {
            "q": query,
            "format": "json",
            "no_html": 1,
            "skip_disambig": 1
        }
        response = req_lib.get(
            "https://api.duckduckgo.com/",
            params=params,
            timeout=10,
        )
        data = response.json()
        results = []

        # Abstract/summary
        abstract = data.get("Abstract", "")
        if abstract:
            results.append("**Summary:** " + abstract)
            abstract_url = data.get("AbstractURL", "")
            if abstract_url:
                results.append("Source: " + abstract_url)

        # Related topics
        related_topics = data.get("RelatedTopics", [])
        for topic in related_topics[:5]:
            if isinstance(topic, dict):
                text = topic.get("Text", "")
                if text:
                    results.append("- " + text)

        if results:
            return "\n".join(results)
        else:
            return _search_duckduckgo_html(query)

    except Exception as e:
        return "Search failed: " + str(e)


def _search_duckduckgo_html(query):
    """Fallback: scrape DuckDuckGo lite results for better coverage."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        response = req_lib.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=headers,
            timeout=10,
        )

        # Extract snippets using regex
        pattern = r'class="result__snippet"[^>]*>(.*?)</a>'
        snippets = re.findall(pattern, response.text, re.DOTALL)

        if snippets:
            results = ["**Web Search Results for:** " + query + "\n"]
            for i, snippet in enumerate(snippets[:5], 1):
                # Clean HTML tags
                clean = re.sub(r"<[^>]+>", "", snippet).strip()
                if clean:
                    results.append(str(i) + ". " + clean)
            return "\n".join(results)

        return "No results found for: " + query

    except Exception as e:
        return "Search failed: " + str(e)


# ===== SEND MESSAGE WITH WEB SEARCH (RESPONSES API) =====
def send_message_with_search(messages, model, enable_search=True):
    """
    Send message to codex via LiteLLM /v1/responses with optional web search.

    Uses OpenAI SDK's responses.create() which LiteLLM routes to Bedrock Mantle
    directly (no internal transformation) since config uses bedrock_mantle/ prefix.

    Returns:
        tuple: (stream_object, used_search_bool)
    """
    client = get_openai_client()
    tools = None
    if enable_search and WEB_SEARCH_ENABLED:
        tools = [WEB_SEARCH_TOOL]

    try:
        # First call - non-streaming to detect tool calls
        first_response = client.responses.create(
            model=model,
            input=messages,
            tools=tools,
            stream=False,
        )

        # Check for tool calls in the response output
        tool_calls = []
        for item in first_response.output:
            if item.type == "function_call":
                tool_calls.append(item)

        if tool_calls:
            # Process web search tool calls
            tool_results_input = messages.copy()

            # Add the function call outputs from the first response
            for tc in tool_calls:
                tool_results_input.append({
                    "type": "function_call",
                    "call_id": tc.call_id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                })

                if tc.name == "web_search":
                    args = json.loads(tc.arguments)
                    search_query = args.get("query", "")

                    # Show search indicator
                    st.caption("🔍 Searching: *" + search_query + "*")

                    # Perform the search
                    search_result = perform_web_search(search_query)

                    tool_results_input.append({
                        "type": "function_call_output",
                        "call_id": tc.call_id,
                        "output": search_result,
                    })

            # Second call - streaming with search context
            stream = client.responses.create(
                model=model,
                input=tool_results_input,
                stream=True,
            )
            return stream, True

        else:
            # No tool call - stream response directly
            stream = client.responses.create(
                model=model,
                input=messages,
                stream=True,
            )
            return stream, False

    except Exception as e:
        raise e


def extract_stream_text(stream):
    """
    Extract text deltas from an OpenAI responses streaming object.
    Yields text chunks as they arrive.
    """
    for event in stream:
        # Handle different event types from the OpenAI SDK responses stream
        if event.type == "response.output_text.delta":
            if hasattr(event, "delta") and event.delta:
                yield event.delta
        elif event.type == "response.text.delta":
            if hasattr(event, "delta") and event.delta:
                yield event.delta
        elif event.type == "response.content_part.delta":
            if hasattr(event, "delta"):
                if isinstance(event.delta, str):
                    yield event.delta
                elif hasattr(event.delta, "text") and event.delta.text:
                    yield event.delta.text
        elif event.type == "response.completed":
            break


# ===== LOCAL STORAGE PERSISTENCE (JavaScript) =====
LOCAL_STORAGE_JS = """
<script>
    function saveChatHistory(sessionId, messages) {
        var key = 'codex_chat_' + sessionId;
        var data = {
            messages: messages,
            timestamp: Date.now(),
            expiry: 30 * 24 * 60 * 60 * 1000
        };
        localStorage.setItem(key, JSON.stringify(data));
    }

    function loadChatHistory(sessionId) {
        var key = 'codex_chat_' + sessionId;
        var stored = localStorage.getItem(key);
        if (stored) {
            var data = JSON.parse(stored);
            var now = Date.now();
            if (now - data.timestamp < data.expiry) {
                return data.messages;
            } else {
                localStorage.removeItem(key);
                return [];
            }
        }
        return [];
    }

    function getAllSessions() {
        var sessions = [];
        for (var i = 0; i < localStorage.length; i++) {
            var key = localStorage.key(i);
            if (key.startsWith('codex_chat_')) {
                var sessionId = key.replace('codex_chat_', '');
                try {
                    var data = JSON.parse(localStorage.getItem(key));
                    if (Date.now() - data.timestamp < data.expiry) {
                        var preview = 'Empty';
                        if (data.messages && data.messages.length > 0) {
                            preview = data.messages[0].content.substring(0, 50);
                        }
                        sessions.push({
                            id: sessionId,
                            timestamp: data.timestamp,
                            preview: preview
                        });
                    } else {
                        localStorage.removeItem(key);
                    }
                } catch(e) {
                    localStorage.removeItem(key);
                }
            }
        }
        sessions.sort(function(a, b) { return b.timestamp - a.timestamp; });
        return sessions;
    }

    function deleteSession(sessionId) {
        localStorage.removeItem('codex_chat_' + sessionId);
    }

    function cleanExpiredSessions() {
        var now = Date.now();
        for (var i = localStorage.length - 1; i >= 0; i--) {
            var key = localStorage.key(i);
            if (key && key.startsWith('codex_chat_')) {
                try {
                    var data = JSON.parse(localStorage.getItem(key));
                    if (now - data.timestamp >= data.expiry) {
                        localStorage.removeItem(key);
                    }
                } catch(e) {
                    localStorage.removeItem(key);
                }
            }
        }
    }

    cleanExpiredSessions();
</script>
"""


# ===== SESSION STATE INITIALIZATION =====
if "messages" not in st.session_state:
    st.session_state["messages"] = []

if "current_session" not in st.session_state:
    st.session_state["current_session"] = datetime.now().strftime("%Y%m%d_%H%M%S")

if "chat_sessions" not in st.session_state:
    st.session_state["chat_sessions"] = []


def save_current_chat():
    """Save current chat to session state chat list."""
    current_id = st.session_state.get("current_session", "")
    messages = st.session_state.get("messages", [])

    if not messages:
        return

    # Get preview from first user message
    preview = "New Chat"
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "New Chat")
            if isinstance(content, str):
                preview = content[:40]
            break

    # Update or add session
    sessions = st.session_state.get("chat_sessions", [])
    updated = False
    for session in sessions:
        if session["id"] == current_id:
            session["preview"] = preview
            session["messages"] = messages.copy()
            session["timestamp"] = time.time()
            updated = True
            break

    if not updated:
        sessions.insert(0, {
            "id": current_id,
            "preview": preview,
            "messages": messages.copy(),
            "timestamp": time.time()
        })

    # Keep only last 20 sessions
    st.session_state["chat_sessions"] = sessions[:20]


def new_chat():
    """Start a new chat session."""
    save_current_chat()
    st.session_state["messages"] = []
    st.session_state["current_session"] = datetime.now().strftime("%Y%m%d_%H%M%S")


# ===== SIDEBAR =====
chat_config = get_chat_config()
user_api_key = st.session_state.get("api_key", "")

with st.sidebar:
    # Header
    st.markdown("## 🤖 codex Chat")
    st.caption("Self-hosted on Amazon Bedrock")
    st.divider()

    # Logout button
    logout_button()
    st.divider()

    # New Chat Button
    if st.button("➕ New Chat", use_container_width=True):
        new_chat()
        st.rerun()

    st.divider()

    # ===== DYNAMIC MODEL SELECTOR =====
    st.markdown("### 🧠 Model")

    if user_api_key:
        available_models = chat_config.get_user_allowed_models(user_api_key)

        if available_models:
            display_names = [get_display_name(m) for m in available_models]

            selected_display = st.selectbox(
                "Select Model",
                options=display_names,
                index=0,
                help="Models assigned to you by your admin",
            )

            selected_model = get_model_alias(selected_display, available_models)
            st.session_state["selected_model"] = selected_model

            model_count = str(len(available_models))
            st.caption(model_count + " model(s) available")
        else:
            st.error("No models available. Contact your admin.")
            st.stop()
    else:
        st.info("Enter your API key to access models.")
        api_key_input = st.text_input("API Key", type="password", key="sidebar_api_key")
        if api_key_input:
            st.session_state["api_key"] = api_key_input
            st.rerun()
        st.stop()

    st.divider()

    # ===== BUDGET DISPLAY =====
    st.markdown("### 💰 Budget")

    col1, col2 = st.columns([4, 1])
    with col2:
        if st.button("🔄", key="refresh_budget", help="Refresh budget info"):
            chat_config._budget_cache = {}
            st.rerun()

    budget_info = chat_config.get_user_budget_info(user_api_key)
    max_budget = budget_info.get("max_budget")

    if max_budget:
        spent = budget_info.get("spend", 0)
        total = max_budget
        remaining = budget_info.get("remaining", 0)

        if total > 0:
            pct = min(spent / total * 100, 100)
        else:
            pct = 0

        st.progress(pct / 100)

        budget_text = (
            "$" + str(round(spent, 2)) +
            " / $" + str(round(total, 2)) +
            " (" + str(round(pct, 1)) + "% used)"
        )
        st.caption(budget_text)

        budget_level = budget_info.get("budget_level", "")
        if budget_level:
            st.caption("📋 Set at: " + budget_level + " level")

        if remaining is not None and remaining < 5:
            warn_text = "⚠️ Only $" + str(round(remaining, 2)) + " remaining!"
            st.warning(warn_text)
    else:
        st.caption("No budget limit set")

    # Rate limits
    tpm = budget_info.get("tpm_limit")
    rpm = budget_info.get("rpm_limit")
    if tpm:
        st.caption("⚡ TPM: " + str(tpm))
    if rpm:
        st.caption("🔄 RPM: " + str(rpm))

    st.divider()

    # ===== WEB SEARCH TOGGLE =====
    st.markdown("### 🌐 Web Search")
    web_search_enabled = st.toggle(
        "Enable web search",
        value=True,
        key="web_search_toggle",
        help="When enabled, codex can search the web for current information",
    )
    st.session_state["web_search_enabled"] = web_search_enabled

    if web_search_enabled:
        st.caption("✅ codex will search the web when needed")
    else:
        st.caption("❌ Offline mode - training data only")

    st.divider()

    # ===== CHAT HISTORY LIST =====
    st.markdown("### 💬 Recent Chats")
    chat_sessions = st.session_state.get("chat_sessions", [])
    if chat_sessions:
        for session in chat_sessions[:10]:
            session_id = session.get("id", "")
            preview = session.get("preview", "Chat")[:40]
            btn_key = "session_" + session_id
            if st.button(preview, key=btn_key):
                save_current_chat()
                st.session_state["current_session"] = session_id
                st.session_state["messages"] = session.get("messages", [])
                st.rerun()
    else:
        st.caption("No chat history yet")


# ===== FILE UPLOAD =====
uploaded_files = st.file_uploader(
    "Attach files",
    type=[
        "pdf", "docx", "doc", "xlsx", "xls", "csv",
        "txt", "md", "py", "js", "ts", "java", "cpp", "c", "go", "rs",
        "html", "css", "json", "yaml", "yml", "xml", "sql",
        "png", "jpg", "jpeg", "gif", "webp",
        "log", "sh", "bash", "env", "toml", "ini", "cfg"
    ],
    accept_multiple_files=True,
    help="Upload files for codex to analyze (max 25MB per file)",
    label_visibility="collapsed",
)


# ===== DISPLAY CHAT HISTORY =====
for msg in st.session_state["messages"]:
    role = msg.get("role", "user")
    content = msg.get("content", "")
    # Only display string content (skip multimodal content blocks in display)
    display_content = content if isinstance(content, str) else "[File attached]"
    with st.chat_message(role):
        st.markdown(display_content)


# ===== CHAT INPUT HANDLER =====
if prompt := st.chat_input("Message codex..."):
    model = st.session_state.get("selected_model", DEFAULT_MODEL)
    web_search_on = st.session_state.get("web_search_enabled", True)

    # Build message content for API
    api_content = []

    # Process file attachments
    if uploaded_files:
        for uploaded_file in uploaded_files:
            file_data = prepare_file_for_api(uploaded_file)
            if file_data:
                api_content.append(file_data)

    api_content.append({"type": "text", "text": prompt})

    # Display user message
    with st.chat_message("user"):
        st.markdown(prompt)
        if uploaded_files:
            for f in uploaded_files:
                st.caption("📎 Attached: " + f.name)

    # Store user message in session (store plain text for display)
    st.session_state["messages"].append({
        "role": "user",
        "content": prompt
    })

    # Build API messages with context (last 20 messages for token efficiency)
    api_messages = []
    recent_messages = st.session_state["messages"][-20:]

    for msg in recent_messages[:-1]:
        api_messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    # Current message (with file attachments if any)
    if len(api_content) > 1:
        # Multi-part content with files
        api_messages.append({"role": "user", "content": api_content})
    else:
        # Simple text message
        api_messages.append({"role": "user", "content": prompt})

    # Stream response from codex via LiteLLM /v1/responses
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""

        try:
            stream, used_search = send_message_with_search(
                api_messages, model, enable_search=web_search_on
            )

            for delta in extract_stream_text(stream):
                full_response += delta
                message_placeholder.markdown(full_response + "▌")

            message_placeholder.markdown(full_response)

            if used_search:
                st.caption("🌐 Response includes web search results")

        except Exception as e:
            error_msg = str(e)
            if "budget" in error_msg.lower():
                full_response = (
                    "⚠️ Usage limit reached. "
                    "Contact your admin to increase your budget."
                )
            elif "rate" in error_msg.lower():
                full_response = (
                    "⚠️ Rate limit hit. "
                    "Please wait a moment and try again."
                )
            elif "model" in error_msg.lower() and "not allowed" in error_msg.lower():
                full_response = (
                    "🚫 Model access denied. "
                    "You do not have permission to use this model. "
                    "Contact your admin."
                )
            elif "invalid api key" in error_msg.lower() or "401" in error_msg:
                full_response = (
                    "🔑 Invalid API key. "
                    "Please logout and re-enter your credentials."
                )
            else:
                full_response = "❌ Error: " + error_msg
            message_placeholder.markdown(full_response)

    # Save assistant response
    st.session_state["messages"].append({
        "role": "assistant",
        "content": full_response
    })
    save_current_chat()


# ===== INJECT LOCAL STORAGE JS =====
st.components.v1.html(LOCAL_STORAGE_JS, height=0)

