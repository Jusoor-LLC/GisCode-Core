"""Debug script to understand the clarification flow."""
from giscode.gisai.engine import GisAiEngine
from giscode.projects.context import ProjectContext
from pathlib import Path

fixture_path = str(Path('giscode/gisai/inspection/test_fixtures/minimal_project'))
ctx = ProjectContext.create(fixture_path, name='test-project')
engine = GisAiEngine(
    model_manager=None,
    vision_service=None,
    coding_agent=None,
    project_context=ctx,
    inference=None,
    task_manager=None,
)

task = engine.receive('delete the database')
print('After receive:')
print(f'  task.state = {task.state}')
print(f'  task.status = {task.status}')
print(f'  task.intent = {task.intent}')
print(f'  task.required_capabilities = {task.required_capabilities}')

result = engine.execute(task)
print('After execute:')
print(f'  task.state = {task.state}')
print(f'  task.status = {task.status}')
print(f'  result = {result}')

# Check what clarification questions were generated
if 'clarification_questions' in result:
    print(f'Clarification questions: {len(result.get("clarification_questions", []))}')
    for q in result.get('clarification_questions', []):
        print(f'  - {q["category"]}: {q["question"][:60]}... (blocking={q["blocking"]})')
else:
    print('No clarification_questions in result')