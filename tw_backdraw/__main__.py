import sys

from .cli import main

try:
    sys.exit(main())
except BrokenPipeError:
    # 輸出被 head/less 之類的下游關掉，屬正常結束
    sys.stderr.close()
    sys.exit(0)
