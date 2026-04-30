import threading
import os
import subprocess
import json
from typing import List, Dict
from src.shared.shared_constants import SENTINEL, logger

class RoslynServer:
    def __init__(self, tool_dir: str):
        self.tool_dir = tool_dir
        self.process = None
        self.lock = threading.Lock()

    def start(self):
        logger.info("Starting Persistent Roslyn Server...")
        # Use the built binary instead of 'dotnet run' for speed and reliability
        exe_path = os.path.join(self.tool_dir, "bin", "Release", "net8.0", "roslyn_tool.exe")
        if not os.path.exists(exe_path):
            exe_path = "dotnet run -c Release" # Fallback
            
        self.process = subprocess.Popen(
            exe_path if os.path.exists(exe_path) else ["dotnet", "run", "-c", "Release"],
            cwd=self.tool_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8", 
            bufsize=1
        )
        # Wait for "READY"
        while True:
            line = self.process.stdout.readline()
            if not line:
                err = self.process.stderr.read()
                logger.error(f"Roslyn Server failed to start. Stderr: {err}")
                break
            if "READY" in line:
                logger.info("Roslyn Server is READY.")
                break

    def _send_command(self, command: str, code: str) -> str:
        with self.lock:
            for attempt in range(2): # Try twice if pipe breaks
                if not self.process or self.process.poll() is not None:
                    self.start()
                
                try:
                    # Send command line then code + sentinel
                    self.process.stdin.write(command + "\n")
                    self.process.stdin.write(code + "\n" + SENTINEL + "\n")
                    self.process.stdin.flush()

                    # Read until sentinel
                    output = []
                    while True:
                        line = self.process.stdout.readline()
                        if not line: 
                            if attempt == 0: break # Try restart
                            return ""
                        line = line.strip("\r\n")
                        if line == SENTINEL: break
                        output.append(line)
                    
                    if line == SENTINEL:
                        return "\n".join(output)
                    
                    self.stop()
                except (BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Roslyn Server connection lost: {e}. Attempting restart...")
                    self.stop()
                except Exception as e:
                    logger.error(f"Roslyn Server Error: {e}")
                    return ""
            return ""

    def clean_code(self, code: str) -> str:
        return self._send_command("CLEAN|||", code)

    def diff_extract(self, old_code: str, new_code: str, old_lns: List[int], new_lns: List[int]) -> List[Dict]:
        old_lns_str = ",".join(map(str, old_lns))
        new_lns_str = ",".join(map(str, new_lns))
        header = f"DIFF_EXTRACT|||{old_lns_str}|||{new_lns_str}"
        combined_code = f"{old_code}\n---DELIMITER---\n{new_code}"
        res = self._send_command(header, combined_code)
        try:
            return json.loads(res) if res else []
        except Exception as e:
            logger.error(f"Failed to parse Roslyn JSON: {e}. Response: {res[:100]}...")
            return []

    def extract_block(self, code: str, line_num: int) -> Dict:
        """Extracts the semantic node covering line_num. Returns {signature, block_code}."""
        header = f"EXTRACT_BLOCK|||{line_num}"
        res = self._send_command(header, code)
        try:
            return json.loads(res) if res else {"signature": "", "block_code": ""}
        except Exception:
            return {"signature": "", "block_code": res or ""}

    def stop(self):
        if self.process:
            logger.info("Stopping Roslyn Server...")
            self.process.terminate()
            self.process = None