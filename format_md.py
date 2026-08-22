import os

directories = ['phase1 qns', 'phase 2 qns', 'phase3']

def format_markdown(text):
    lines = text.split('\n')
    formatted = []
    in_json_block = False
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        # Headers
        if stripped in ['Problem Details', 'Answer Criteria', 'Scoring', 'Run summary page', 'Others', 'Example', 'Example:', 'Input Format', 'Output Format', 'Constraints', 'Notes', 'Batch Request Format', 'Batch Example', 'Timeout']:
            formatted.append('\n## ' + stripped.replace(':', ''))
        elif stripped.startswith('Problem Set'):
            formatted.append('\n### ' + stripped)
        elif stripped.startswith(('Phase ', 'Stage ')):
            formatted.append('\n# ' + stripped)
        elif stripped in ['Request:', 'Response:', 'Input', 'Output']:
            formatted.append('\n#### ' + stripped.replace(':', ''))
            
        # JSON code blocks
        elif stripped == '{' and not in_json_block:
            in_json_block = True
            formatted.append('```json')
            formatted.append(line)
        elif stripped == '}' and in_json_block:
            formatted.append(line)
            # Peek ahead to see if JSON continues (e.g. `},`)
            if i + 1 >= len(lines) or not lines[i+1].strip().startswith(','):
                in_json_block = False
                formatted.append('```')
                
        else:
            formatted.append(line)
            
    # Fix any open JSON blocks
    if in_json_block:
        formatted.append('```')
        
    return '\n'.join(formatted)

for d in directories:
    if os.path.exists(d):
        for f in os.listdir(d):
            if f.endswith('.md'):
                path = os.path.join(d, f)
                with open(path, 'r') as file:
                    content = file.read()
                formatted_content = format_markdown(content)
                with open(path, 'w') as file:
                    file.write(formatted_content)
print("Finished adding markdown syntax.")
