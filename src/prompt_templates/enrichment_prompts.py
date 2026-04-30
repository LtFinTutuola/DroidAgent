SYSTEM_PROMPT = (
    "You are an AI Data Engineer expert in creating Instruction-Tuning datasets (Alpaca/Instruct format). "
    "Your task is to generate a single, direct imperative instruction that a developer would write as a prompt to request the SPECIFIC code modification shown in the diff. "
    "CRITICAL RULES TO AVOID OVERFITTING AND DICTATION: "
    "1. The instruction MUST identify the specific method/class changed by reading its signature EXACTLY as it appears in the [CODE DIFF]. NEVER extract the method/property name from the Commit description. "
    "2. When identifying the component, mention the class or file name NATURALLY, as a human developer would in a chat. DO NOT use full file paths. (e.g., BAD: 'Update Dispose in folder/path/Control.cs'. GOOD: 'Update the Dispose method in the Control class'). "
    "3. DO NOT simply copy or repeat the Commit description. Use the Commit context ONLY to understand the business intent. "
    "4. NEVER include literal code snippets, operators (e.g., '==', '>='), or exact syntax in your instruction. Explain the goal conceptually so the future AI learns to write the code itself. "
    "5. Start immediately with an imperative action verb (e.g., Update, Fix, Add, Refactor, Remove, Implement). "
    "6. Do NOT use conversational or passive language. "
    "7. Keep it strictly to 1 or 2 concise sentences maximum."
)

USER_PROMPT_CSHARP = """\
Analyze the provided Commit context, the file name, and the raw code modifications. 
Synthesize the ideal user prompt/instruction that would command THIS EXACT software change.

CRITICAL CONSTRAINTS:
- Identify the exact Method or Property name from the [CODE DIFF] signature. 
- Use the provided `file_name` to deduce the name of the class or component being modified. Incorporate this name NATURALLY into the instruction to avoid ambiguity (e.g., "in the Bitmap class"). Do NOT output the raw file path.
- Bridge the gap between Business Intent and Technical Implementation. Analyze the [CODE DIFF] to understand WHAT changed, but use the [COMMIT CONTEXT] to understand WHY.
- Frame the instruction as a logical problem to solve, NOT as a dictation of keystrokes.
- If `raw_old_code` is empty or missing, formulate it as a creation request: "Add the [ExactNameFromDiff] method/property to the [ClassName] class to handle [Business Logic]".
- Output ONLY the imperative instruction.

[COMMIT CONTEXT]
commit_description: {commit_description}

[FILE INFO]
file_name: {file_name}

[CODE DIFF]
raw_old_code: 
{raw_old_code}

raw_new_code: 
{raw_new_code}
"""

USER_PROMPT_XAML = """\
Analyze the provided Commit context, the file name, and the raw XML modifications. 
Synthesize the ideal user prompt/instruction that would command THIS EXACT UI/markup change.

CRITICAL CONSTRAINTS:
- Identify the UI control or markup element being changed from the [CODE DIFF]. 
- Use the provided `file_name` to deduce the name of the view, page, or layout component being modified. Incorporate this name NATURALLY into the instruction to avoid ambiguity (e.g., "in the UserProfile view"). Do NOT output the raw file path.
- Bridge the gap between Business Intent and Technical Implementation. Use UI/Markup terminology (e.g., "Update the layout", "Modify the UI control", "Adjust the visual property"). Analyze the [CODE DIFF] to understand WHAT changed, but use the [COMMIT CONTEXT] to understand WHY.
- Frame the instruction as a logical UI change to implement.
- If `raw_old_code` is empty or missing, formulate it as an addition: "Add the [ElementName] control to the [ViewName] to display [Business Logic]".
- Output ONLY the imperative instruction.

[COMMIT CONTEXT]
commit_description: {commit_description}

[FILE INFO]
file_name: {file_name}

[CODE DIFF]
raw_old_code: 
{raw_old_code}

raw_new_code: 
{raw_new_code}
"""

USER_PROMPT_CSPROJ = """\
Analyze the provided Commit context, the file name, and the raw project file modifications. 
Synthesize the ideal user prompt/instruction that would command THIS EXACT architectural or dependency change.

CRITICAL CONSTRAINTS:
- Identify the exact dependency, build property, or architectural setting changed in the [CODE DIFF]. 
- Use the provided `file_name` to deduce the name of the project. Incorporate this name NATURALLY into the instruction to avoid ambiguity (e.g., "in the Core project"). Do NOT output the raw file path.
- Bridge the gap between Business Intent and Technical Implementation. Use architectural terminology (e.g., "Add the dependency", "Update the build property", "Link the package").
- Frame the instruction as an architectural or build-system goal.
- If `raw_old_code` is empty or missing, formulate it as an addition.
- Output ONLY the imperative instruction.

[COMMIT CONTEXT]
commit_description: {commit_description}

[FILE INFO]
file_name: {file_name}

[CODE DIFF]
raw_old_code: 
{raw_old_code}

raw_new_code: 
{raw_new_code}
"""
