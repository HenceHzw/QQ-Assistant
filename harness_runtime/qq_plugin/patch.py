"""Expose the official plugin's bot to our business bridge; no protocol changes."""
import json
from pathlib import Path

root = Path(__file__).parent / 'node_modules/@tencent-connect/dsh-qqbot'
assert json.loads((root / 'package.json').read_text())['version'] == '0.5.5'
changes = [
    ('lib/index.js', "export const inject = ['agents'];", "export const inject = ['agents', 'qqAssistantBridge'];"),
    ('lib/gateway/bootstrap.js',
        '    setupMiddlewares(bot, config, manager, logger);',
        '    ctx.qqAssistantBridge.attach(bot, config);\n    setupMiddlewares(bot, config, manager, logger);'),
    ('lib/gateway/bootstrap.js',
        '    registerSendFileTool(ctx, mediaSender, manager, config, logger);',
        '    // QQ Assistant handles delivery; do not expose file sending to the model.'),
]
for name, before, after in changes:
    path = root / name
    source = path.read_text()
    if after in source:
        continue
    assert source.count(before) == 1, f'Official plugin interface changed: {name}'
    path.write_text(source.replace(before, after))
