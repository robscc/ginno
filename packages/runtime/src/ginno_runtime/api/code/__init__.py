"""Code panel endpoints — read-only workspace browsing (docs/code-panel-design.md).

The panel addresses files BY ROOT + RELATIVE PATH (never an absolute path):
a *root* is either a mounted context folder (``f_xxxx``) or the session
workspace (``session``). ``roots``/``list``/``read`` are S1, ``write`` is S2,
git decoration is S3, and ``search`` (name ⌘P / content ⇧⌘F) is S4.

Fence model (design §4.6): every request runs through ``resolve_code_target``,
which applies the gates in order — root lookup, containment, symlink
re-resolution, the permanent deny regions, and (for writes) the ``ro`` tier /
``.git`` rule. Gate 4 mirrors ``tools/builtin._path_denied`` EXACTLY: the
exempt base is the SESSION WORKSPACE dir, never ``primary_path`` (a mount lives
outside ``~/.ginno``, so passing it silently disables the workspace exemption
and the session-workspace root comes back empty).

Responses follow the ``{ok, ...}`` shape used by ``files.py``/``folders.py``
(HTTP 200 even on failure); errors carry an appended-only ``code`` the UI maps
to copy.

Package layout (重构批次 2-2: 从单模块 ``api/code.py`` 绞杀式拆分,分节注释与
代码体逐字迁移,语义零变化;本 __init__ 聚合 re-export 原 import 路径与全部
顶层名字,下游 import 点零改动):

  _common   共享常量、``CodeAccessError``、``router`` 本体、wire 错误、
            session 上下文解析、§4.6 fence、共享解码
  _read     S1 roots/list/read + live diff (code-panel-diff-design.md §6.2)
  _write    S2 write (原子保存 + 版本冲突拒写)
  _bytes    raw 字节流 + preview file server (HTML sandbox iframe)
  _git      S3 git 装饰 (status/numstat 端点 + HEAD blob 读取)
  _search   S4 name/content 搜索

``_CodePackage.__setattr__`` 在文件末尾把 ``setattr(api.code, name, ...)``
同步回定义该名字的子模块,恢复单模块时代 monkeypatch 的穿透语义。
"""

from __future__ import annotations

import sys
import types

# Import order mirrors the endpoints' original definition order in code.py
# (the OpenAPI contract snapshot is order-insensitive, but keep it natural).
from . import _read, _write, _bytes, _git, _search  # noqa: F401
from . import _common  # noqa: F401  (imported last: base, no endpoints)
from ._bytes import (  # noqa: F401
    _CSP_TMPL,
    _HOST_RE,
    _PREVIEW_MIME,
    _RAW_MIME,
    RAW_MAX_BYTES,
    _raw_error,
    code_file,
    code_raw,
)
from ._common import (  # noqa: F401
    BINARY_SNIFF_BYTES,
    EDITABLE_MAX_BYTES,
    HEAVY_DIRS,
    HIDDEN_COUNT_CAP,
    LIST_MAX_ENTRIES,
    MAX_DIFF_BYTES,
    PREVIEW_MAX_BYTES,
    PREVIEW_MAX_LINES,
    CodeAccessError,
    _ENCODINGS,
    _LANGUAGES,
    _decode,
    _err,
    _git_info,
    _list_roots,
    _mount_dirs,
    _norm_rel,
    _rel_parts,
    _root_from_dir,
    _session_ctx,
    _unknown_session,
    _write_state,
    resolve_code_target,
    router,
)
from ._git import (  # noqa: F401
    GIT_TIMEOUT_S,
    _EMPTY_GIT,
    _XY_PRECEDENCE,
    _git_numstat,
    _git_prefix,
    _git_run,
    _git_toplevel,
    _parse_status,
    _read_head,
    _relocate,
    _relocate_numstat,
    _root_offset,
    _xy_letter,
    code_git,
)
from ._read import (  # noqa: F401
    _count_immediate,
    _detect_encoding,
    _entry,
    _language_for,
    _list_dir,
    code_diff,
    code_list,
    code_read,
    code_roots,
)
from ._search import (  # noqa: F401
    SEARCH_CONTENT_MAX_DIRS,
    SEARCH_CONTENT_MAX_HITS,
    SEARCH_CONTENT_TIME_BUDGET_S,
    SEARCH_LIMIT_DEFAULT,
    SEARCH_LIMIT_MAX,
    SEARCH_MAX_FILE_BYTES,
    SEARCH_MAX_QUERY_BYTES,
    SEARCH_NAME_MAX_ENTRIES,
    SEARCH_NAME_TIME_BUDGET_S,
    _name_rank,
    _search_content,
    _search_name,
    _subsequence,
    code_search,
)
from ._write import code_write  # noqa: F401

#: 子模块全集 — ``__setattr__`` 转发的目标域(按定义先后)。
_SUBMODULES = (_read, _write, _bytes, _git, _search, _common)


class _CodePackage(types.ModuleType):
    """让 ``setattr(api.code, name, value)`` 穿透到定义 ``name`` 的子模块。

    单模块时代 ``monkeypatch.setattr(code, "_git_run", ...)`` 替换的是模块
    全局,端点内部的裸名调用随之走 fake;拆包后端点在子模块里查自己的
    globals,只改包属性不再生效(tests/unit/test_code_*.py 依赖此语义)。
    这里把对包的每次赋值同步进仍持有该名字的子模块;monkeypatch teardown
    的恢复赋值同样经过本 ``__setattr__``,因此不会跨测试残留。

    仅覆盖赋值:读取走上方显式 re-export,包属性新增(名字原本不在任何
    子模块)也只落在包上,行为与普通模块一致。
    """

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for sub in _SUBMODULES:
            if name in vars(sub):
                setattr(sub, name, value)


sys.modules[__name__].__class__ = _CodePackage
