🏢 Secure Insurance Policy RAG (Vectorless)
A private, local-first Retrieval Augmented Generation (RAG) system designed to answer complex insurance benefit questions across multiple years and plans without exposing confidential PDFs to the cloud.

🌟 Why "Vectorless" RAG?
Unlike traditional RAG that converts text into mathematical "vectors" (which can be fuzzy and lose context), this system uses a Surgical Indexing approach:
Structural Accuracy: It maps documents by Year, Plan Type, and Page Number.
Zero Data Leakage: PDFs remain on your local disk. Text is only read "surgically" when the AI specifically requests a page.
Parallel Comparison: The agentic "Brain" can autonomously trigger multiple searches to compare 2024 vs 2025 data.

🛠️ Project Structure
text
/my-secure-rag/
├── .env                       # Local configuration (Paths & Model)
├── create_test_docs.py        # 1. Generates dummy insurance PDFs for testing
├── indexer.py                 # 2. Builds the "Surgical" JSON maps
├── server.py                  # 3. The Secure Tool (MCP Server)
├── client.py                  # 4. The Agentic Logic (Ollama Bridge)
├── app.py                     # 5. The Gradio Web Interface
├── docs/                      # Source PDF Folder (Organized by Year)
└── indices/                   # Generated JSON Maps

🚀 Setup Instructions

1. Prerequisites
Python 3.10+ installed.
Ollama Desktop installed and running (ollama.com).
Model: Download the tool-capable model:
bash
ollama pull llama3.1

2. Installation
Install the required Python libraries:
bash
pip install fastmcp pypdf ollama gradio python-dotenv reportlab mcp

3. Configuration (.env)
Create a .env file in the root folder with the following:
env
DOC_BASE_DIR="./docs"
INDEX_OUTPUT_DIR="./indices"
MASTER_INDEX_FILE="master_metadata.json"
OLLAMA_MODEL="llama3.1"

📖 Execution Sequence
Step 1: Create Test Documents
Generate "dummy" insurance booklets to verify the system works.
bash
python create_test_docs.py

Step 2: Build the Surgical Index
The local LLM will "read" the PDFs and create a JSON Table of Contents.
bash
python indexer.py

Verify that master_metadata.json and files in /indices are created.
Step 3: Launch the Web UI
Start the browser-based assistant.
bash
python app.py

Open http://127.0.0.1:7860 in your browser.
🧠 How it Works (Technical Flow)
User Asks: "Compare my 2024 and 2025 Gold medical deductibles."
Intent Detection: The Client (Llama 3.1) identifies two distinct years and plans.
Tool Call: The Client triggers two parallel calls to query_insurance_benefits.
Surgical Fetch: The Python script locates the exact page in the docs/ folder using the JSON index.
Synthesis: The LLM receives the raw text from both pages and writes the final comparison.

🛡️ Security & Confidentiality
Local Processing: No data is sent to OpenAI or Anthropic. All reasoning happens via Ollama on your hardware.
No Embedding DB: Avoids the "Black Box" of vector databases. You can read the JSON index to see exactly what is mapped.
Audit Log: Every file access is triggered by a specific Python function, which can be easily logged for compliance.

🛠️ Troubleshooting & FAQ
1. "TaskGroup" or Connection Errors
Symptoms: The Web UI shows System Error: unhandled errors in a TaskGroup or a connection timeout.
Cause: Windows "Pipes" are blocking the communication between the Client and the Server scripts.
Solution:
Ensure the SERVER_PATH in client.py is an absolute path (e.g., C:\AI\server.py).
Check that server.py has zero print() statements. MCP uses the terminal's "Standard Output" to send data; any extra text will "pollute" the connection and cause a crash.
Restart the Ollama Desktop App to clear any hung background processes.

2. "Ollama: Term not recognized"
Symptoms: You try to run ollama pull and the terminal says it cannot find the command.
Cause: Ollama was installed but not added to your Windows System PATH.
Solution:
Close and Restart VS Code (this refreshes the terminal's environment).
If it still fails, use the full path in the terminal: & "$env:LOCALAPPDATA\ollama\ollama.exe" pull llama3.1.

3. "Error 400: Model does not support tools"
Symptoms: The UI says status code: 400 when you ask a question.
Cause: You are using the original llama3 model, which cannot handle "Surgical" tool calling.
Solution: Update your .env to OLLAMA_MODEL="llama3.1" and ensure you have run ollama pull llama3.1.

4. Indexer Fails to Create JSON Files
Symptoms: master_metadata.json is empty or the indices/ folder has no files.
Cause: The local LLM is failing to return valid JSON during the "Reasoning" step.
Solution:
Check that the Ollama App is open and active in your taskbar.
Delete the empty master_metadata.json and try running python indexer.py again. It should show [*] Indexing Page-by-Page... for each PDF.

5. "ModuleNotFoundError: pypdf" (or others)
Symptoms: You run a script and it says a library is missing, even after pip install.
Cause: VS Code is using a different Python "Interpreter" than the one where you installed the libraries.
Solution:
Press Ctrl + Shift + P in VS Code.
Type "Python: Select Interpreter".
Choose the one marked "Recommended" or the one that matches your python --version in the terminal.

🔒 Security Best Practices for Production
Firewall: Ensure the Gradio UI (port 7860) is only accessible within your company VPN.
Audit Logging: The server.py can be updated to write every query_insurance_benefits call to a secure SQL database for compliance tracking.
Read-Only Docs: Set the ./docs folder to "Read-Only" for the Python process to ensure the AI can never accidentally modify or delete a policy document.



#Prompt to check
1. Compare my specialist copay between the 2024 Gold Medical plan and the 2026 Premera Gold HMO. Please show the result in a Markdown table.

2. In the 2026 Premera Gold HMO, what is my cost for an X-ray if I stay In-Network versus if I go Out-of-Network?

3. Create a table comparing the individual annual deductible for the 2024 Gold Medical, 2025 Gold Medical, and 2026 Premera Gold HMO plans.

4. What are the benefits for 'Emergency Room' services in the 2026 Premera Gold HMO? Are there any specific copays or coinsurance?

5. Compare the individual annual deductible for 2024 Gold Medical vs 2026 Premera Gold HMO. Please use a table.

6. Create a table comparing the individual annual deductible for 2024 Gold Medical vs 2026 Premera Gold HMO.

7. Compare the individual annual deductible for 2024 Gold vs 2026 Premera Gold. Show the values in a table.