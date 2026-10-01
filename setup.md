# Setup

This guide starts from a fresh clone and runs the local browser app.

## Prerequisites

- [Git](https://git-scm.com/downloads)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A Groq API key for real story generation

## 1. Clone the repository

Replace `<repository-url>` with the clone URL for this repository:

```sh
git clone <repository-url>
cd KuKuFMTask
```

Run the remaining commands from the folder containing `generate.py` and `pyproject.toml`.

## 2. Install dependencies

```sh
uv sync
```

This creates the local Python environment and installs the project dependencies.

## 3. Configure the API key

Copy `.env.example` to `.env`, then open `.env` in a text editor and replace the placeholder `GROQ_API_KEY` with your key.

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
```

On macOS or Linux:

```sh
cp .env.example .env
${EDITOR:-nano} .env
```

Keep `.env` private; do not commit it or share its contents.

## 4. Launch the browser app

```sh
uv run generate.py ui
```

Open **http://localhost:8765** in your browser. Keep the terminal window open while using the app. Stop the server with **Ctrl+C**.

## 5. Create or resume a story

In the app, enter a premise to start a new story, review the arc plan, and approve it before writing episodes. You can review, edit, reject, or provide feedback on episodes. The app saves story state in `runs/`; open the same run after restarting to continue it. Create a separately named run for another premise.

## Optional: try the UI without an API key

Mock mode lets you explore the app workflow without making model calls. Set `LLM_PROVIDER=mock` for the process before starting the app.

PowerShell:

```powershell
$env:LLM_PROVIDER = "mock"
uv run generate.py ui
```

macOS or Linux:

```sh
LLM_PROVIDER=mock uv run generate.py ui
```

Mock mode is for checking the workflow; it does not demonstrate representative story quality.

## Optional: use the CLI

The same story workflow is available from the terminal:

```sh
uv run generate.py --premise "A delivery rider realizes every address on today's route belongs to someone who died in the same building." --run rider
uv run generate.py plan --run rider
uv run generate.py create 5 --run rider
uv run generate.py status --run rider
```

Reuse a run name to continue that story, or choose another name for a separate story. For the full command list, run `uv run generate.py --help`.
