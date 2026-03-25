import os, ollama, json, re
from dotenv import load_dotenv

load_dotenv()
LOCAL_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

# --- GLOBAL RAM CACHE (Persistent for the session) ---
TOOL_RESULT_CACHE = {}

def flatten_message_content(content):
    """
    NUCLEAR NORMALIZER: Forces any Ollama response (List, Dict, or None) 
    into a plain string to prevent Gradio/Streamlit/Pydantic validation errors.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get('text', str(item)))
            else:
                parts.append(str(item))
        return " ".join(parts).strip()
    return str(content).strip()

async def get_ai_response(query, history):
    try:
        from server import query_insurance_benefits, get_available_plans 
        
        # --- 1. CONTEXT MERGING (MEMORY) ---
        recent_history = " ".join([flatten_message_content(m['content']) for m in history])
        full_context_query = f"{recent_history} {query}".lower()

        # --- 2. EXTRACTION WITH TOPIC PROTECTION ---
        # A. Plan Type: SMART PIVOT
        new_type = "medical" 
        if any(w in query.lower() for w in ["dental", "ortho", "braces"]): new_type = "dental"
        elif any(w in query.lower() for w in ["vision", "eye"]): new_type = "vision"
        elif any(w in query.lower() for w in ["pcp", "doctor", "copay", "medical"]): new_type = "medical"
        else:
            if "dental" in full_context_query: new_type = "dental"
            elif "vision" in full_context_query: new_type = "vision"
            else: new_type = "medical"

        # B. Years: Look in current query first, then history
        found_years = re.findall(r'202\d', query)
        if not found_years:
            found_years = sorted(list(set(re.findall(r'202\d', full_context_query))))
            if not found_years: found_years = ["2025"]

        # C. Tier: THE SMART PERSISTENCE FIX
        # We only scavenge 'Gold' from history if the Plan Type hasn't changed.
        # This prevents forcing 'Gold' onto a 'Silver' dental plan.
        p_tier_match = re.search(r'(gold|silver|bronze)', query.lower())
        
        # Determine previous type mentioned in history
        last_type = "medical"
        if history:
            last_msg = next((m['content'].lower() for m in reversed(history) if m['role'] == 'user'), "")
            if "dental" in last_msg: last_type = "dental"
            elif "vision" in last_msg: last_type = "vision"

        if not p_tier_match:
            if new_type == last_type:
                # Same topic? Scavenge the whole history
                p_tier_match = re.search(r'(gold|silver|bronze)', full_context_query)
            else:
                # NEW Topic? Do NOT scavenge history tiers (sets to None for Discovery)
                p_tier_match = None
        
        p_tier_fast = p_tier_match.group() if p_tier_match else (None if new_type == "dental" else "gold")
        p_type_fast = new_type

        print(f"[*] RESOLVED CONTEXT: Years={found_years}, Type={p_type_fast}, Tier={p_tier_fast}")

        # --- 3. TURBO CACHE HIT CHECK ---
        cached_data_fragments = []
        if found_years and p_tier_fast: # Only cache hit if we have a valid tier
            for y in found_years:
                key = f"{y}_{p_type_fast}_{p_tier_fast}".replace(" ", "").lower()
                if key in TOOL_RESULT_CACHE:
                    cached_data_fragments.append(TOOL_RESULT_CACHE[key])

        # If we have ALL requested years in cache, skip the LLM loop!
        if len(cached_data_fragments) == len(found_years) and len(found_years) > 0:
            print(f"[*] TURBO CACHE HIT: Bypassing reasoning for {found_years}")
            # --- THE FINAL SAFETY NET (INTEGRATED INTO CACHE HIT) ---
            instruction = (
                f"You are a professional Insurance Advisor. Plan: {p_tier_fast} {p_type_fast}. "
                "Synthesize the data accurately. If comparing years, use a Markdown Table. "
                "Provide specific values ($25, $35, etc.) found in the text. No JSON."
            )
            fast_msgs = [
                {'role': 'system', 'content': instruction},
                {'role': 'user', 'content': f"Synthesize this data: {str(cached_data_fragments)} for query: {query}"}
            ]
            final_synth = ollama.chat(model=LOCAL_MODEL, messages=fast_msgs, options={"temperature": 0.3})
            return flatten_message_content(final_synth['message'].get('content', ''))

        # --- 4. INITIAL DISCOVERY CHECK ---
        called_discovery = True if not p_tier_fast else False
            
        # --- 5. THE REASONING LOOP ---
        messages = []
        system_prompt = {
        'role': 'system',
        'content': (
            "You are a specialized Insurance Assistant. Goal: 100% Accuracy."
            "\n\nSTRICT TOOL RULES:"
            "\n1. TOPIC ISOLATION: If the user switches Plan Types (e.g., from Medical to Dental), "
            "you MUST NOT use the Year or Tier from the previous topic. Forget the previous context."
            "\n2. DISCOVERY FIRST: For a new Plan Type, always call 'get_available_plans' "
            "before 'query_insurance_benefits' to confirm exactly which Years and Tiers exist."
            "\n3. COMPARISON: If asked to compare years (e.g., 2024 vs 2025), you MUST generate a "
            "SEPARATE tool call for EACH year mentioned."
            "\n4. NO GUESSING: Never provide a 'year' or 'plan_tier' in a tool call "
            "unless it was explicitly in the CURRENT query or confirmed via 'get_available_plans'."
            "\n5. SILENCE: Provide ONLY JSON tool blocks during planning. No conversational filler."
            "\n6. NO NARRATION: Do NOT tell the user you are calling a tool. Output ONLY JSON."
        )
    }

        messages.append(system_prompt)

        if history:
            for turn in history[-2:]:
                messages.append({'role': turn.get('role', 'user'), 'content': flatten_message_content(turn.get('content', ''))})

        messages.append({'role': 'user', 'content': query})

        tools = [
		{'type': 'function', 'function': {'name': 'get_available_plans', 'description': 'PROBE DB index for available years/tiers'}},
		{
            'type': 'function', 
            'function': {
                'name': 'query_insurance_benefits',
                'description': 'Retrieve benefit text. Only call this if you have a SPECIFIC Year and Tier.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'year': {'type': 'integer', 'description': 'The 4-digit year. Use 0 if unknown.'},
                        'plan_type': {'type': 'string'},
                        'plan_tier': {'type': 'string', 'description': 'Gold, Silver, or Bronze. Use "Unknown" if not specified.'},
                        'topic': {'type': 'string'}
                    },
                    "required": ["topic"] # Remove year and tier from the 'required' list
                }
            }
        }
	]

       
        # --- THE REASONING LOOP ---
        turn_count = 0
        final_raw_context = "" 

        while turn_count < 3:
            turn_count += 1
            resp = ollama.chat(model=LOCAL_MODEL, messages=messages, tools=tools, options={"temperature": 0})
            
            raw_msg = resp['message']
            clean_content = flatten_message_content(raw_msg.get('content', ''))
            tool_calls = raw_msg.get('tool_calls', [])

            messages.append({'role': 'assistant', 'content': clean_content, 'tool_calls': tool_calls if tool_calls else None})
            
            if not tool_calls:
                if clean_content and len(clean_content.strip()) > 20:
                    return clean_content
                break 

            for call in tool_calls:
                func_name = call['function']['name']
                args = call['function']['arguments']
                
                if func_name == "get_available_plans":
                    called_discovery = True
                    result = get_available_plans()
                    messages.append({'role': 'tool', 'content': str(result), 'name': func_name})
                    
                    # --- LIVE REFRESH (Optional but helpful) ---
                    res_str = str(result).lower()
                    if not p_tier_fast:
                        t_match = re.search(r"'(gold|silver|bronze)'", res_str)
                        if t_match: p_tier_fast = t_match.group(1)

                elif func_name == "query_insurance_benefits":
                    # --- THE FIX: REMOVE THE 'DEFERRING/SKIP' CHECK ---
                    # We let the tool call proceed even if p_tier_fast is None.
                    # The LLM will either provide the tier in 'args' or the tool will return an error.
                    
                    all_args_blob = str(args).lower()
                    years_to_process = re.findall(r'202\d', all_args_blob) or found_years or ["2025"]
                    
                    results_list = []
                    for p_year in years_to_process:
                        # 1. REMOVE THE 'UNKNOWN' DEFAULT
                        # Use the scavenger value (p_tier_fast) or the LLM's arg, but leave empty if neither exist.
                        p_type = p_type_fast or args.get('plan_type', 'medical').lower()
                        p_tier = p_tier_fast or args.get('plan_tier', '').lower()
                        
                        # 2. DYNAMIC TOPIC MAPPING (Matches 'orthodontia' in your PDF)
                        if any(w in query.lower() for w in ["dental", "ortho", "braces"]):
                            p_topic = "orthodontia"
                        else:
                            p_topic = "benefit"

                        print(f"[*] SURGICAL FETCH: Year={p_year}, Type={p_type}, Tier={p_tier or 'ANY'}")
                        
                        # 3. EXECUTE (Letting the server handle the empty Tier by searching all Tiers)
                        data = query_insurance_benefits(
                            year=int(p_year), 
                            plan_type=p_type, 
                            plan_tier=p_tier.capitalize() if p_tier else None, 
                            topic=p_topic
                        )
                        results_list.append(data)
                            
                        final_raw_context = "\n\n".join(results_list)
                        messages.append({'role': 'tool', 'content': final_raw_context, 'name': func_name})
        
                        
        # --- THE FINAL SAFETY NET (REFINED FOR DOCLING/MARKDOWN) ---
        print("[*] TRIGGERING FINAL COMPREHENSIVE SYNTHESIS...")
        
        # 1. CONSOLIDATE DATA: Merge Tool Results + Cache
        master_context = []
        reasoning_data = [m['content'] for m in messages if m.get('role') == 'tool']
        if reasoning_data:
            master_context.append("\n".join(reasoning_data))
        if cached_data_fragments:
            master_context.append("\n".join(cached_data_fragments))

        # 2. CLEAN SLATE: Rebuild to kill 'JSON noise' and focus on Markdown
        final_messages = [
            {'role': 'system', 'content': "You are a high-precision Insurance Advisor. You answer based ONLY on the provided MASTER DATA."},
            {'role': 'user', 'content': f"MASTER DATA (Structured Markdown):\n{str(master_context)}\n\nUSER QUESTION: {query}"}
        ]

                # --- 3. DYNAMIC INSTRUCTION: THE IRONCLAD PROTOCOL ---      
        if master_context and "not found" not in str(master_context).lower():
            instruction = (
                f"You are the Data Analyst for the {p_tier_fast} {p_type_fast} plans. "
                "### MANDATORY OUTPUT RULES:"
                "\n1. NO APOLOGIES: Never say 'Error' or 'Unable to retrieve'. If data exists in the MASTER DATA, you MUST present it as fact."
                "\n2. NO GUESSING: Use ONLY the numbers found in the MASTER DATA. Do NOT use numbers like $1,500 or $2,000 unless they are explicitly in the provided text."
                "\n3. TABLE ONLY: For any query involving multiple years or In/Out of Network columns, you MUST output a Markdown Table. No introductory text."
                "\n4. NO JSON: Do not show tool calls or code blocks."
                "\n5. START IMMEDIATELY: Begin your response directly with the data table."
            )
        else:
            from server import get_available_plans
            instruction = f"DB SCHEMA: {str(get_available_plans())}. No data found. Identify valid years/tiers only."

        final_messages.insert(0, {'role': 'system', 'content': instruction})
        
        # 4. FINAL EXECUTION: Set temperature to 0.0 for absolute rigidity
        final_resp = ollama.chat(
            model=LOCAL_MODEL, 
            messages=final_messages, 
            options={"temperature": 0.0} 
        )
        return flatten_message_content(final_resp['message'].get('content', ''))


    except Exception as e:
        return f"⚠️ Client Logic Error: {str(e)}"



