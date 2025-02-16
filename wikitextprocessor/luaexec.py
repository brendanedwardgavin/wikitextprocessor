# Helper functions for interfacing with the Lua sandbox for executing Lua
# macros in Wikitext (Wiktionary, Wikipedia, etc.)
#
# Copyright (c) Tatu Ylonen.  See file LICENSE and https://ylonen.org

import copy
import os
import re
import functools
import html
import json
import traceback
import unicodedata
import pkg_resources
import multiprocessing # XXX debug, remove me

from typing import Optional

import lupa.lua51 as lupa
from .parserfns import PARSER_FUNCTIONS, call_parser_function, tag_fn

# List of search paths for Lua libraries.
builtin_lua_search_paths = [
    # [path, ignore_modules]
    [".", ["string", "debug"]],
    ["mediawiki-extensions-Scribunto/includes/engines/LuaCommon/lualib", []],
]

# Determine which directory our data files are in
lua_dir = pkg_resources.resource_filename("wikitextprocessor", "lua/")
if not lua_dir.endswith("/"):
    lua_dir += "/"
# print("lua_dir", lua_dir)


def lua_loader(ctx: "Wtp", modname: str) -> Optional[str]:
    """This function is called from the Lua sandbox to load a Lua module.
    This will load it from either the user-defined modules on special
    pages or from a built-in module in the file system.  This returns None
    if the module could not be loaded."""
    # print("LUA_LOADER IN PYTHON:", modname)
    assert isinstance(modname, str)
    modname = modname.strip()
    ns_data = ctx.NAMESPACE_DATA["Module"]
    ns_prefix = ns_data["name"] + ":"
    ns_alias_prefixes = tuple(alias + ":" for alias in ns_data["aliases"])
    if modname.startswith(ns_alias_prefixes):
        modname = ns_prefix + modname[modname.find(":") + 1 :]

    # Local name usually not used in Lua code
    if modname.startswith(("Module:", ns_prefix)):
        # First try to load it as a module
        if modname.startswith(("Module:_", ns_prefix + "_")):
            # Module names starting with _ are considered internal and cannot be
            # loaded from the dump file for security reasons.  This is to ensure
            # that the sandbox always gets loaded from a local file.
            data = None
        else:
            data = ctx.read_by_title(modname, ns_data["id"])
    else:
        # Try to load it from a file
        path = modname
        path = re.sub(r"[\0-\037]", "", path)  # Remove control chars, e.g. \n
        path = path.replace(":", "/")
        path = path.replace(" ", "_")
        path = re.sub(r"//+", "/", path)  # Replace multiple slashes by one
        path = re.sub(r"\.\.+", ".", path)  # Replace .. and longer by .
        path = re.sub(r"^//+", "", path)  # Remove initial slashes
        path += ".lua"
        data = None
        for prefix, exceptions in builtin_lua_search_paths:
            if modname in exceptions:
                continue
            p = lua_dir + prefix + "/" + path
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = f.read()
                break

    return data


def mw_text_decode(text, decodeNamedEntities):
    """Implements the mw.text.decode function for Lua code."""
    if decodeNamedEntities:
        return html.unescape(text)

    # Otherwise decode only selected entities
    parts = []
    pos = 0
    for m in re.finditer(r"&(lt|gt|amp|quot|nbsp);", text):
        if pos < m.start():
            parts.append(text[pos : m.start()])
        pos = m.end()
        tag = m.group(1)
        if tag == "lt":
            parts.append("<")
        elif tag == "gt":
            parts.append(">")
        elif tag == "amp":
            parts.append("&")
        elif tag == "quot":
            parts.append('"')
        elif tag == "nbsp":
            parts.append("\xa0")
        else:
            assert False
    parts.append(text[pos:])
    return "".join(parts)


def mw_text_encode(text, charset):
    """Implements the mw.text.encode function for Lua code."""
    parts = []
    for ch in str(text):
        if ch in charset:
            chn = ord(ch)
            if chn in html.entities.codepoint2name:
                parts.append("&" + html.entities.codepoint2name.get(chn) + ";")
            else:
                parts.append(ch)
        else:
            parts.append(ch)
    return "".join(parts)


def mw_text_jsondecode(ctx, s, *rest):
    flags = rest[0] if rest else 0
    value = json.loads(s)

    def recurse(x):
        if isinstance(x, (list, tuple)):
            return ctx.lua.table_from(list(map(recurse, x)))
        if not isinstance(x, dict):
            return x
        # It is a dict.
        if (flags & 1) == 1:
            # JSON_PRESERVE_KEYS flag means we don't convert keys.
            return ctx.lua.table_from({k: recurse(v) for k, v in x.items()})
        # Convert numeric keys to integers and see if we can make it a
        # table with sequential integer keys.
        for k, v in list(x.items()):
            if k.isdigit():
                del x[k]
                x[int(k)] = recurse(v)
            else:
                x[k] = recurse(v)
        if not all(isinstance(k, int) for k in x.keys()):
            return ctx.lua.table_from(x)
        keys = list(sorted(x.keys()))
        if not all(keys[i] == i + 1 for i in range(len(keys))):
            return ctx.lua.table_from(x)
        # Old unused print value? XXX remove this if you can't figure out
        # what it's for.
        # values = list(x[i + 1] for i in range(len(keys)))
        return ctx.lua.table_from(x)

    value = recurse(value)
    return value


def mw_text_jsonencode(s, *rest):
    flags = rest[0] if rest else 0

    def recurse(x):
        if isinstance(x, (str, int, float, type(None), type(True))):
            return x
        if lupa.lua_type(x) == "table":
            conv_to_dict = (flags & 1) != 0  # JSON_PRESERVE_KEYS flag
            if not conv_to_dict:
                # Also convert to dict if keys are not sequential integers
                # starting from 1
                if not all(isinstance(k, int) for k in x.keys()):
                    conv_to_dict = True
                else:
                    keys = list(sorted(x.keys()))
                    if not all(keys[i] == i + 1 for i in range(len(keys))):
                        conv_to_dict = True
            if conv_to_dict:
                ht = {}
                for k, v in x.items():
                    ht[str(k)] = recurse(v)
                return ht
            # Convert to list (JSON array)
            return list(map(recurse, x.values()))
        return x

    value = recurse(s)
    return json.dumps(value, sort_keys=True)


def get_page_info(ctx: "Wtp", title: str):
    """Retrieves information about a page identified by its table (with
    namespace prefix.  This returns a lua table with fields "id", "exists",
    and "redirectTo".  This is used for retrieving information about page
    titles."""
    page_id = 0  # XXX collect required info in phase 1
    page = ctx.get_page(title)
    # whether the page exists and what its id might be
    dt = {
        "id": page_id,
        "exists": page is not None,
        "redirectTo": page.redirect_to if page is not None else None,
    }
    return ctx.lua.table_from(dt)


def get_page_content(ctx: "Wtp", title: str) -> Optional[str]:
    """Retrieves the full content of the page identified by the title.
    Currently this will only return content for the current page.
    This returns None if the page is other than the current page, and
    False if the page does not exist (currently not implemented)."""

    # Read the page by its title
    return ctx.read_by_title(title.strip())


def fetch_language_name(ctx, code):
    """This function is called from Lua code as part of the mw.language
    implementation.  This maps a language code to its name."""
    return ctx.LANGUAGES_BY_CODE.get(code)


def fetch_language_names(ctx, include):
    """This function is called from Lua code as part of the mw.language
    implementation.  This returns a list of known language names."""
    include = str(include)
    if include == "all":
        ret = ctx.LANGUAGES_BY_CODE
    else:
        ret = {"en": ctx.LANGUAGES_BY_CODE["en"]}
    return ctx.lua.table_from(ret)


def call_set_functions(ctx, set_functions):
    def debug_mw_text_jsondecode(x, *rest):
        return mw_text_jsondecode(ctx, x, *rest)

    def debug_get_page_info(x, *args):
        if args:
            print(f"LAMBDA GET_PAGE_INFO DEBUG:"
                  f" {repr(args)},"
                  f" {ctx.title=},"
                  f" {multiprocessing.current_process().name}")
        return get_page_info(ctx, x)

    def debug_get_page_content(x, *args):
        if args:
            print(f"LAMBDA GET_PAGE_CONTENT DEBUG:"
                  f" {repr(args)},"
                  f" {ctx.title=},"
                  f" {multiprocessing.current_process().name}")
        return get_page_content(ctx, x)

    def debug_fetch_language_name(x, *args):
        if args:
            print(f"LAMBDA FETCH_LANGUAGE_NAME DEBUG:"
                  f" {repr(args)},"
                  f" {ctx.title=},"
                  f" {multiprocessing.current_process().name}")
        return fetch_language_name(ctx, x)

    def debug_fetch_language_names(x, *args):
        if args:
            print(f"LAMBDA FETCH_LANGUAGE_NAMES DEBUG:"
                  f" {repr(args)},"
                  f" {ctx.title=},"
                  f" {multiprocessing.current_process().name}")
        return fetch_language_names(ctx, x)
    # Set functions that are implemented in Python
    set_functions(
        mw_text_decode,
        mw_text_encode,
        mw_text_jsonencode,
        # lambda x, *rest: mw_text_jsondecode(ctx, x, *rest),
        # lambda x: get_page_info(ctx, x),
        # lambda x: get_page_content(ctx, x),
        # lambda x: fetch_language_name(ctx, x),
        # lambda x: fetch_language_names(ctx, x),
        debug_mw_text_jsondecode,
        debug_get_page_info,
        debug_get_page_content,
        debug_fetch_language_name,
        debug_fetch_language_names,
        mw_wikibase_getlabel,
        mw_wikibase_getdescription,
    )


def initialize_lua(ctx):
    def filter_attribute_access(obj, attr_name, is_setting):
        if isinstance(attr_name, str) and not attr_name.startswith("_"):
            return attr_name
        raise AttributeError("access denied")

    lua = lupa.LuaRuntime(
        unpack_returned_tuples=True,
        register_eval=False,
        attribute_filter=filter_attribute_access,
    )
    ctx.lua = lua
    set_namespace_data = lua.eval("function(v) NAMESPACE_DATA = v end")
    lua_namespace_data = copy.deepcopy(ctx.NAMESPACE_DATA)
    for ns_name, ns_data in lua_namespace_data.items():
        for k, v in ns_data.items():
            if isinstance(v, list):
                lua_namespace_data[ns_name][k] = lua.table_from(v)
        lua_namespace_data[ns_name] = lua.table_from(
            lua_namespace_data[ns_name]
        )
    set_namespace_data(lua.table_from(lua_namespace_data))

    # Load Lua sandbox Phase 1.  This is a very minimal file that only sets
    # the Lua loader to our custom loader; we will then use it to load the
    # bigger phase 2 of the sandbox.  This way, most of the sandbox loading
    # will benefit from caching and precompilation (when implemented).
    with open(lua_dir + "_sandbox_phase1.lua", encoding="utf-8") as f:
        phase1_result = lua.execute(f.read())
        set_loader = phase1_result[1]
        clear_loaddata_cache = phase1_result[2]
        # Call the function that sets the Lua loader
        set_loader(lambda x: lua_loader(ctx, x))

    # Then load the second phase of the sandbox.  This now goes through the
    # new loader and is evaluated in the sandbox.  This mostly implements
    # compatibility code.
    ret = lua.eval('new_require("_sandbox_phase2")')
    set_functions = ret[1]
    ctx.lua_invoke = ret[2]
    ctx.lua_reset_env = ret[3]
    ctx.lua_clear_loaddata_cache = clear_loaddata_cache

    # Set Python functions for Lua
    call_set_functions(ctx, set_functions)


def call_lua_sandbox(ctx, invoke_args, expander, parent, timeout):
    """Calls a function in a Lua module in the Lua sandbox.
    ``invoke_args`` is the arguments to the call; ``expander`` should
    be a function to expand an argument.  ``parent`` should be None or
    (parent_title, parent_args) for the parent page."""
    assert isinstance(invoke_args, (list, tuple))
    assert callable(expander)
    assert parent is None or isinstance(parent, (list, tuple))
    assert timeout is None or isinstance(timeout, (int, float))

    # print("{}: CALL_LUA_SANDBOX: {} {}"
    #       .format(ctx.title, invoke_args, parent))

    if len(invoke_args) < 2:
        ctx.debug(
            "#invoke {} with too few arguments".format(invoke_args),
            sortid="luaexec/369",
        )
        return "{{" + invoke_args[0] + ":" + "|".join(invoke_args[1:]) + "}}"

    # Initialize the Lua sandbox if not already initialized
    if ctx.lua_depth == 0:
        if ctx.lua is None:
            # This is the first call to the Lua sandbox.
            # Create a Lua context and initialize it.
            initialize_lua(ctx)  # This sets ctx.lua
        else:
            # This is a second or later call to the Lua sandbox.
            # Reset the Lua context back to initial state.
            ctx.lua_reset_env()
            ret = ctx.lua.eval('new_require("_sandbox_phase2")')
            set_functions = ret[1]
            ctx.lua_invoke = ret[2]
            ctx.lua_reset_env = ret[3]
            call_set_functions(ctx, set_functions)

    ctx.lua_depth += 1
    lua = ctx.lua

    # Get module and function name
    modname = expander(invoke_args[0]).strip()
    modfn = expander(invoke_args[1]).strip()

    def value_with_expand(frame, fexpander, x):
        assert isinstance(frame, dict)
        assert isinstance(fexpander, str)
        assert isinstance(x, str)
        obj = {"expand": lambda obj: frame[fexpander](x)}
        return lua.table_from(obj)

    def make_frame(pframe, title, args):
        assert isinstance(title, str)
        assert isinstance(args, (list, tuple, dict))
        # Convert args to a dictionary with default value None
        if isinstance(args, dict):
            frame_args = {}
            for k, arg in args.items():
                arg = re.sub(r"(?si)<\s*noinclude\s*/\s*>", "", arg)
                arg = html.unescape(arg)
                frame_args[k] = arg
        else:
            assert isinstance(args, (list, tuple))
            frame_args = {}
            num = 1
            for arg in args:
                m = re.match(r"""(?s)^\s*([^<>="']+?)\s*=\s*(.*?)\s*$""", arg)
                if m:
                    # Have argument name
                    k, arg = m.groups()
                    if k.isdigit():
                        k = int(k)
                        if k < 1 or k > 1000:
                            k = 1000
                        if num <= k:
                            num = k + 1
                else:
                    # No argument name
                    k = num
                    num += 1
                # Remove any <noinclude/> tags; they are used to prevent
                # certain token interpretations in Wiktionary
                # (e.g., Template:cop-fay-conj-table), whereas Lua code
                # does not always like them (e.g., remove_links() in
                # Module:links).
                arg = re.sub(r"(?si)<\s*noinclude\s*/\s*>", "", arg)
                arg = html.unescape(arg)
                frame_args[k] = arg
        frame_args = lua.table_from(frame_args)

        def extensionTag(frame, *args):
            if len(args) < 1:
                ctx.debug(
                    "lua extensionTag with missing arguments",
                    sortid="luaexec/464",
                )
                return ""
            dt = args[0]
            if not isinstance(dt, (str, int, float, type(None))):
                name = str(dt["name"] or "")
                content = str(dt["content"] or "")
                attrs = dt["args"] or {}
            elif len(args) == 1:
                name = str(args[0])
                content = ""
                attrs = {}
            elif len(args) == 2:
                name = str(args[0] or "")
                content = str(args[1] or "")
                attrs = {}
            else:
                name = str(args[0] or "")
                content = str(args[1] or "")
                attrs = args[2] or {}
            if not isinstance(attrs, str):
                attrs = list(
                    v
                    if isinstance(k, int)
                    else '{}="{}"'.format(k, html.escape(v, quote=True))
                    for k, v in sorted(attrs.items(), key=lambda x: str(x[0]))
                )
            elif not attrs:
                attrs = []
            else:
                attrs = [attrs]

            ctx.expand_stack.append("extensionTag()")
            ret = tag_fn(
                ctx, "#tag", [name, content] + attrs, lambda x: x
            )  # Already expanded
            ctx.expand_stack.pop()
            # Expand any templates from the result
            ret = preprocess(frame, ret)
            return ret

        def callParserFunction(frame, *args):
            if len(args) < 1:
                ctx.debug(
                    "lua callParserFunction missing name", sortid="luaexec/506"
                )
                return ""
            name = args[0]
            if not isinstance(name, str):
                new_args = name["args"]
                if isinstance(new_args, str):
                    new_args = {1: new_args}
                else:
                    new_args = dict(new_args)
                name = name["name"] or ""
            else:
                new_args = []
            name = str(name)
            for arg in args[1:]:
                if isinstance(arg, (int, float, str)):
                    new_args.append(str(arg))
                else:
                    for k, v in sorted(arg.items(), key=lambda x: str(x[0])):
                        new_args.append(str(v))
            name = ctx._canonicalize_parserfn_name(name)
            if name not in PARSER_FUNCTIONS:
                ctx.debug(
                    "lua frame callParserFunction() undefined "
                    "function {!r}".format(name),
                    sortid="luaexec/529",
                )
                return ""
            return call_parser_function(ctx, name, new_args, lambda x: x)

        def expand_all_templates(encoded):
            # Expand all templates here, even if otherwise only
            # expanding some of them.  We stay quiet about undefined
            # templates here, because Wiktionary Module:ugly hacks
            # generates them all the time.
            ret = ctx.expand(encoded, parent, quiet=True)
            return ret

        def preprocess(frame, *args):
            if len(args) < 1:
                ctx.debug(
                    "lua preprocess missing argument", sortid="luaexec/545"
                )
                return ""
            v = args[0]
            if not isinstance(v, str):
                v = str(v["text"] or "")
            # Expand all templates, in case the Lua code actually
            # inspects the output.
            v = ctx._encode(v)
            ctx.expand_stack.append("frame:preprocess()")
            ret = expand_all_templates(v)
            ctx.expand_stack.pop()
            return ret

        def expandTemplate(frame, *args):
            if len(args) < 1:
                ctx.debug(
                    "lua expandTemplate missing arguments", sortid="luaexec/561"
                )
                return ""
            dt = args[0]
            if isinstance(dt, (int, float, str, type(None))):
                ctx.debug(
                    "lua expandTemplate arguments should be named",
                    sortid="luaexec/566",
                )
                return ""
            title = dt["title"] or ""
            args = dt["args"] or {}
            new_args = [title]
            for k, v in sorted(args.items(), key=lambda x: str(x[0])):
                new_args.append("{}={}".format(k, v))
            encoded = ctx._save_value("T", new_args, False)
            ctx.expand_stack.append("frame:expandTemplate()")
            ret = expand_all_templates(encoded)
            ctx.expand_stack.pop()
            return ret

        def debugGetParent(ctx, *args):
            if args:
                print(f"LAMBDA GETPARENT DEBUG (title: {title}): {repr(args)}"
                      f", process: {multiprocessing.current_process().name}")
            return pframe

        def debugGetTitle(ctx, *args):
            if args:
                print(f"LAMBDA GETTITLE DEBUG: (title: {title}): {repr(args)}"
                      f", process: {multiprocessing.current_process().name}")
            return title

        def debugNewParserValue(ctx, x):
            return value_with_expand(ctx, "preprocess", x)

        def debugNewTemplateParserValue(ctx, x):
            return value_with_expand(ctx, "expand", x)

        # Create frame object as dictionary with default value None
        frame = {}
        frame["args"] = frame_args
        # argumentPairs is set in sandbox.lua
        frame["callParserFunction"] = callParserFunction
        frame["extensionTag"] = extensionTag
        frame["expandTemplate"] = expandTemplate
        # getArgument is set in sandbox.lua
        frame["getParent"] = debugGetParent
        frame["getTitle"] = debugGetTitle
        # frame["getParent"] = lambda ctx: pframe
        # frame["getTitle"] = lambda ctx: title
        frame["preprocess"] = preprocess
        # XXX still untested:
        frame["newParserValue"] = debugNewParserValue
        frame["newTemplateParserValue"] = debugNewTemplateParserValue
        # frame["newParserValue"] = lambda ctx, x: value_with_expand(
        #     ctx, "preprocess", x
        # )
        # frame["newTemplateParserValue"] = lambda ctx, x: value_with_expand(
        #     ctx, "expand", x
        # )
        # newChild set in sandbox.lua
        return lua.table_from(frame)

    # Create parent frame (for page being processed) and current frame
    # (for module being called)
    if parent is not None:
        parent_title, page_args = parent
        expanded_key_args = {}
        for k, v in page_args.items():
            if isinstance(k, str):
                expanded_key_args[expander(k)] = v
            else:
                expanded_key_args[k] = v
        pframe = make_frame(None, parent_title, expanded_key_args)
    else:
        pframe = None
    frame = make_frame(pframe, modname, invoke_args[2:])

    # Call the Lua function in the given module
    stack_len = len(ctx.expand_stack)
    ctx.expand_stack.append("Lua:{}:{}()".format(modname, modfn))
    try:
        ret = ctx.lua_invoke(modname, modfn, frame, ctx.title, timeout)
        if not isinstance(ret, (list, tuple)):
            ok, text = ret, ""
        elif len(ret) == 1:
            ok, text = ret[0], ""
        else:
            ok, text = ret[0], ret[1]
    except UnicodeDecodeError:
        ctx.debug(
            "invalid unicode returned from lua by {}: parent {}".format(
                invoke_args, parent
            ),
            sortid="luaexec/626",
        )
        ok, text = True, ""
    except lupa.LuaError as e:
        ok, text = False, e
    finally:
        while len(ctx.expand_stack) > stack_len:
            ctx.expand_stack.pop()
    # print("Lua call {} returned: ok={!r} text={!r}"
    #       .format(invoke_args, ok, text))
    ctx.lua_depth -= 1
    if ok:
        if text is None:
            text = ""
        text = str(text)
        text = unicodedata.normalize("NFC", text)
        return text
    if isinstance(text, Exception):
        parts = [str(text)]
        # traceback.format_exception does not have a named keyvalue etype=
        # anymore, in latest Python versions it is positional only.
        lst = traceback.format_exception(
            type(text), value=text, tb=text.__traceback__
        )
        for x in lst:
            parts.append("\t" + x.strip())
        text = "\n".join(parts)
    elif not isinstance(text, str):
        text = str(text)
    msg = re.sub(r".*?:\d+: ", "", text.split("\n", 1)[0])
    if "'debug.error'" in text:
        if not msg.startswith("This template is deprecated."):
            ctx.debug("lua error -- " + msg, sortid="luaexec/659")
    elif "Translations must be for attested and approved " in text:
        # Ignore this error - it is an error but a clear error in Wiktionary
        # rather than in the extractor.
        return ""
    elif (
        "attempt to index a nil value (local 'lang')" in text
        and "in function 'Module:links.getLinkPage'" in text
    ):
        # Ignore this error - happens when an unknown language code is passed
        # to various templates (a Wiktionary error, not extractor error)
        return ""
    else:
        if "check deprecated lang param usage" in ctx.expand_stack:
            ctx.debug(
                "LUA error but likely not bug -- in #invoke {} parent {}".format(
                    invoke_args, parent
                ),
                trace=text,
                sortid="luaexec/679",
            )
        else:
            ctx.error(
                "LUA error in #invoke {} parent {}".format(invoke_args, parent),
                trace=text,
                sortid="luaexec/683",
            )
    msg = "Lua execution error"
    if "Lua timeout error" in text:
        msg = "Lua timeout error"
    return (
        '<strong class="error">{} in Module:{} function {}'
        "</strong>".format(msg, html.escape(modname), html.escape(modfn))
    )


@functools.cache
def query_wikidata(item_id: str):
    import requests

    r = requests.get(
        "https://query.wikidata.org/sparql",
        params={
            "query": "SELECT ?itemLabel ?itemDescription WHERE { VALUES ?item "
            + f"{{ wd:{item_id} }}. "
            + "SERVICE wikibase:label { bd:serviceParam wikibase:language"
            + ' "[AUTO_LANGUAGE],en". }}',
            "format": "json",
        },
        headers={"user-agent": "wikitextprocessor"},
    )

    if r.ok:
        print(f"WIKIDATA QUERY succeded: {item_id}")
        result = r.json()
        for binding in result.get("results", {}).get("bindings", []):
            return binding
    else:
        print(f"WIKIDATA QUERY failed: {item_id}")
        return None


def mw_wikibase_getlabel(item_id: str) -> str:
    item_data = query_wikidata(item_id)
    return item_data.get("itemLabel", {}).get("value", item_id)


def mw_wikibase_getdescription(item_id: str) -> str:
    item_data = query_wikidata(item_id)
    return item_data.get("itemDescription", {}).get("value", item_id)
