#!/usr/bin/env python3
"""GitHub Copilot 認証スクリプト

Usage:
  python3 src/scripts/copilot_auth.py

認証が完了するとトークンが保存され、デーモンが自動的に利用できるようになります。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import litellm

litellm.suppress_debug_info = False

print("GitHub Copilot 認証を開始します...")
print("ブラウザで表示されたコードを https://github.com/login/device に入力してください\n")

try:
    r = litellm.completion(
        model="github_copilot/claude-haiku-4.5",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=5,
    )
    print("\n✅ 認証成功！デーモンはそのまま利用できます。")
except Exception as e:
    print(f"\n❌ 認証失敗: {e}")
    sys.exit(1)
