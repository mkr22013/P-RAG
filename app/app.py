import streamlit as st
import asyncio
from client import get_ai_response

# --- PAGE CONFIG ---
st.set_page_config(page_title="Insurance Policy Assistant", page_icon="🏢", layout="centered")

st.title("🏢 Insurance Policy Assistant")
st.markdown("Ask about Medical, Dental, or Vision plans (2024-2025-2026).")

# --- INITIALIZE SESSION STATE ---
if "messages" not in st.session_state:
    st.session_state.messages = []

# --- DISPLAY CHAT HISTORY ---
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# --- CHAT INPUT ---
if prompt := st.chat_input("e.g., How did my Gold medical deductible change from 2024 to 2025?"):
    
    # 1. Display user message
    with st.chat_message("user"):
        st.markdown(prompt)
    
    # 2. Add to history
    st.session_state.messages.append({"role": "user", "content": prompt})

    # 3. Generate AI Response
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                # We pass the prompt and the CURRENT history to the client
                # Streamlit uses the same list-of-dicts format as your client logic
                response = asyncio.run(get_ai_response(prompt, st.session_state.messages[:-1]))
                
                st.markdown(response)
                
                # 4. Add Assistant response to history
                st.session_state.messages.append({"role": "assistant", "content": response})
                
            except Exception as e:
                error_msg = f"⚠️ System Error: {str(e)}"
                st.error(error_msg)
