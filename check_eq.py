initial = """19063488C Added portrait mode check edit

Cherry picked from !17008

Related work items: #67165"""

body = """19063488C Added portrait mode check edit

Related work items: #67165"""

print("Are they equal after strip?", body.strip() == initial.strip())
print("---initial stripped---")
print(repr(initial.strip()))
print("---body stripped---")
print(repr(body.strip()))
