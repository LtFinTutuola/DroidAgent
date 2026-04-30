import threading
import subprocess
from src.shared.shared_constants import logger

class GitBatcher:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.process = None
        self.lock = threading.Lock()

    def start(self):
        logger.info("Starting Git Batch Reader (cat-file)...")
        self.process = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=self.repo_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False, # Use binary for cat-file
            bufsize=0
        )

    def get_file_content(self, commit_hash: str, filepath: str) -> str:
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self.start()
            
            try:
                # git cat-file --batch expects "<sha1>:<path>\n"
                query = f"{commit_hash}:{filepath}\n".encode("utf-8")
                self.process.stdin.write(query)
                self.process.stdin.flush()

                # Response format: "<sha> <type> <size>\n<contents>\n"
                header_line = self.process.stdout.readline()
                if not header_line:
                    return ""
                
                header = header_line.decode("utf-8").strip()
                if "missing" in header or not header:
                    # Log if it's not a known case like a deleted file
                    if "missing" not in header:
                        logger.warning(f"Git Batcher unexpected header: '{header}' for {commit_hash}:{filepath}")
                    return ""
                
                parts = header.split()
                if len(parts) < 3: 
                    logger.warning(f"Git Batcher malformed header: '{header}'")
                    return ""
                
                try:
                    size = int(parts[2])
                    
                    bytes_read = 0
                    chunks = []
                    while bytes_read < size:
                        chunk = self.process.stdout.read(size - bytes_read)
                        if not chunk: break
                        chunks.append(chunk)
                        bytes_read += len(chunk)
                    
                    content = b"".join(chunks)
                    
                    # Thoroughly consume the trailing newline
                    terminator = self.process.stdout.read(1)
                    if terminator != b"\n":
                        logger.warning(f"Git Batcher expected \\n after contents, got {terminator}")
                    
                    return content.decode("utf-8", errors="replace")
                except ValueError:
                    logger.error(f"Git Batcher size parse error in header: '{header}'")
                    # Desync alert! We need to restart the process to clear the pipes
                    self.stop()
                    return ""
            except Exception as e:
                logger.error(f"Git Batcher Error: {e}")
                self.stop()
                return ""

    def stop(self):
        if self.process:
            logger.info("Stopping Git Batcher...")
            self.process.terminate()
            self.process = None