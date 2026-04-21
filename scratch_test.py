import sys
import sys
import os

sys.path.append("c:\\DroidAgent\\DroidAgent")
from agent import RoslynServer

server = RoslynServer("c:\\DroidAgent\\DroidAgent\\roslyn_tool")
server.start()

fragment1 = """public void Method() {
    var x = new DevExpress.XtraEditors.TextEdit();
    // old logic
}"""

fragment2 = """public void Method() {
    var x = new CustomControl();
    // new logic
}"""

print("F1 cleaned:")
print(server.clean_code(fragment1))
print("---")
print("F2 cleaned:")
print(server.clean_code(fragment2))
server.stop()
