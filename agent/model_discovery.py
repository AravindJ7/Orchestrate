import os
import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

console = Console()

def list_nvidia_models():
    api_key = os.getenv("NVIDIA_API_KEY")
    base_url = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", [])
    except Exception as e:
        console.print(f"[red]Error fetching models: {e}[/red]")
        return []

def show_free_models():
    models = list_nvidia_models()
    if not models:
        console.print("[yellow]No models returned[/yellow]")
        return
    
    table = Table(title="NVIDIA NIM Models (available for this key)")
    table.add_column("ID", style="cyan")
    table.add_column("Owned By", style="magenta")
    table.add_column("Object", style="green")
    
    for m in models:
        table.add_row(m.get("id", ""), m.get("owned_by", ""), m.get("object", ""))
    
    console.print(table)
    console.print("\n[bold]Tip:[/bold] Pick one with good tool/function calling (often Llama/Nemotron/Mistral). Set it in .env as NVIDIA_MODEL=...")

if __name__ == "__main__":
    show_free_models()
