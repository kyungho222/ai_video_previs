"""Static checks for generated Blender code. Never imports or executes that code."""

import ast
import json
import re
from functools import lru_cache
from pathlib import Path


def apply_function_repairs(code, replacements, allowed_names):
    """Replace selected module functions while preserving every other source byte."""
    tree = ast.parse(code)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    edits = []
    seen = set()
    lines = code.splitlines(keepends=True)
    for replacement in replacements:
        name, source = replacement["name"], replacement["code"]
        if name not in functions or name not in allowed_names or name in seen:
            raise ValueError("Repair must identify one affected existing function")
        parsed = ast.parse(source)
        if (
            len(parsed.body) != 1
            or not isinstance(parsed.body[0], (ast.FunctionDef, ast.AsyncFunctionDef))
            or parsed.body[0].name != name
        ):
            raise ValueError("Return one complete function per replacement")
        node = functions[name]
        if ast.dump(parsed.body[0].args) != ast.dump(node.args):
            raise ValueError("Preserve the existing function interface")
        if [ast.dump(item) for item in parsed.body[0].decorator_list] != [
            ast.dump(item) for item in node.decorator_list
        ]:
            raise ValueError("Preserve the existing function decorators")
        start = min([node.lineno, *[item.lineno for item in node.decorator_list]]) - 1
        edits.append((start, node.end_lineno, source.rstrip() + "\n"))
        seen.add(name)
    if not edits:
        raise ValueError("No function repairs returned")
    for start, end, source in sorted(edits, reverse=True):
        lines[start:end] = [source]
    result = "".join(lines)
    compile(result, "<repaired-previs>", "exec")
    return result


VECTOR_ATTRIBUTES = frozenset(
    {
        "x",
        "y",
        "z",
        "length",
        "length_squared",
        "normalize",
        "normalized",
        "lerp",
        "dot",
        "cross",
        "to_track_quat",
    }
)
TUPLE_HELPERS = frozenset(
    {
        "_pv_add",
        "_pv_sub",
        "_pv_mul",
        "_pv_mix",
        "_pv_unit",
        "_pv_rotate",
        "previs_camera_curve_sample",
        "previs_mirror_point",
    }
)
UNKNOWN = frozenset({"unknown"})
MOTION_MARKER_KEYS = frozenset({"L_Hand", "R_Hand", "L_Knee", "R_Knee"})
MOTION_MARKER_CONTRACT = (
    "previs_register_motion(root, markers) requires a dictionary with exactly "
    "L_Hand, R_Hand, L_Knee, R_Knee keys and actual Blender objects as values. "
    "Example: {'L_Hand':parts['L_hand'],'R_Hand':parts['R_hand'],"
    "'L_Knee':parts['L_knee'],'R_Knee':parts['R_knee']}. Do not pass lists, strings or coordinates. "
    "Choose required_markers explicitly from these four names according to the user action: "
    "a one-hand greeting may require only R_Hand; whole-body dance generally requires all four. "
    "Keep all four real objects registered, but do not animate unrelated knees just to satisfy a check. "
    "Do not choose an empty set or omit genuinely requested action joints."
)
JSON_LOG_POLICY = (
    "For generated diagnostic logs use import json; "
    "print(json.dumps({'event':'camera_check','travel_m':travel,'coverage_pct':coverage*100}, ensure_ascii=False)). "
    "Keep numeric values as JSON numbers, not formatted strings. Do not use percent formatting, str.format, "
    "f-strings or text prefixes for print logs. Use f-strings for object names when needed."
)


@lru_cache(maxsize=1)
def previs_api_contract():
    """Describe the shipped helper API without importing Blender or generated code."""
    sources = {
        "motion_library.py": ("previs_register_motion", "previs_humanoid", "_pv_two_bone"),
        "stage_motion.py": ("previs_camera_register_subjects", "previs_camera_smooth", "previs_validate_visibility"),
        "camera_scale.py": ("previs_camera_from_scale",),
        "cloud_prelude.py": ("render_previs",),
    }
    functions = {}
    for filename, names in sources.items():
        source = Path(__file__).parent / "resources" / filename
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name not in names:
                continue
            positional = [*node.args.posonlyargs, *node.args.args]
            required_count = len(positional) - len(node.args.defaults)
            required = [arg.arg for arg in positional[:required_count]]
            required.extend(arg.arg for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
                            if default is None)
            functions[node.name] = {
                "signature": node.name + "(" + ast.unparse(node.args) + ")",
                "requiredArguments": required,
            }
        if set(names) - functions.keys():
            raise ValueError("Missing previs API definitions in " + filename)
    functions["previs_register_motion"].update({
        "root": "Actual character root Blender Object",
        "markers": {
            "type": "dict[str, Blender Object]",
            "requiredKeys": sorted(MOTION_MARKER_KEYS),
            "additionalKeys": False,
            "values": "Actual objects following the respective hand or knee joints",
            "example": "{'L_Hand': parts['hand_l'], 'R_Hand': parts['hand_r'], "
                       "'L_Knee': parts['knee_l'], 'R_Knee': parts['knee_r']}",
        },
        "required_markers": "Optional nonempty unique subset of the four registered keys, chosen by action",
    })
    functions["previs_humanoid"]["returns"] = "Rig dictionary with root and parts"
    functions["_pv_two_bone"]["returns"] = "Two joint-position tuples; convert to Vector before vector operations"
    functions["previs_camera_register_subjects"]["types"] = {
        "camera": "Blender camera Object", "subjects": "List of actual subject root Objects",
    }
    return json.dumps({
        "version": 1,
        "rules": [
            "Use the supplied parameter names and types when calling these helpers.",
            "Use only the helpers needed by the scene. Keep arbitrary character part names internally.",
            "Construct a separate four-key registration dictionary. Adding aliases to a full rig dictionary is invalid.",
            "Do not redefine, monkey-patch or introspect these helpers to bypass their input contract.",
        ],
        "functions": functions,
    }, ensure_ascii=False)


def _marker_mapping_tags(keys, invalid_values=False):
    tags = {"mapping", "mapping_keys_known", *("marker_key:" + key for key in keys)}
    if set(keys) != MOTION_MARKER_KEYS:
        tags.add("marker_keys_invalid")
    if invalid_values:
        tags.add("marker_values_invalid")
    return frozenset(tags)


def _percent_arity(text):
    count = 0
    index = 0
    while index < len(text):
        if text[index] != "%":
            index += 1
            continue
        if text[index : index + 2] == "%%":
            index += 2
            continue
        match = re.match(
            r"%[#0\- +]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?[diouxXeEfFgGcrsa]", text[index:]
        )
        if not match:
            return None  # Named mapping or unknown specifier: no arity claim.
        count += 1 + match[0].count("*")
        index += len(match[0])
    return count


def validate_generated_code(code, *, forbidden_helpers=()):
    if not isinstance(code, str) or not code.strip():
        return [{"code": "EMPTY_CODE", "line": 1, "message": "Generate nonempty Python code."}]
    if len(code) > 512_000:
        return [
            {
                "code": "CODE_TOO_LARGE",
                "line": 1,
                "message": "Split the Python work into smaller operations.",
            }
        ]
    try:
        tree = ast.parse(code)
        compile(tree, "<previs-validation>", "exec")
    except (SyntaxError, ValueError, RecursionError) as error:
        return [
            {
                "code": "PYTHON_SYNTAX",
                "line": getattr(error, "lineno", None) or 1,
                "message": getattr(error, "msg", None) or type(error).__name__,
            }
        ]
    issues = []
    json_modules = {"json"}
    json_functions = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            json_modules.update(a.asname or a.name for a in node.names if a.name == "json")
        elif isinstance(node, ast.ImportFrom) and node.module == "json":
            json_functions.update(a.asname or a.name for a in node.names if a.name == "dumps")
    scene_names = {"scene", "scn"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "scene"
        ):
            scene_names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    forbidden = set(forbidden_helpers)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in forbidden
        ):
            issues.append(
                {
                    "code": "FIXED_MOTION_PRESET",
                    "line": node.lineno,
                    "message": f"{node.func.id} imposes predefined motion. Author the requested subject motion and camera separately.",
                }
            )

    def infer(node, env):
        if isinstance(node, ast.Name):
            return env.get(node.id, UNKNOWN)
        if isinstance(node, (ast.Tuple, ast.List)):
            tags = {"tuple"}
            if isinstance(node, ast.Tuple) and not any(
                isinstance(n, ast.Starred) for n in node.elts
            ):
                tags.add("tuple_arity:" + str(len(node.elts)))
            return frozenset(tags)
        if isinstance(node, ast.Constant):
            tags = {"scalar"}
            if isinstance(node.value, str):
                arity = _percent_arity(node.value)
                if arity is not None:
                    tags.add("percent_arity:" + str(arity))
            return frozenset(tags)
        if isinstance(node, ast.Dict):
            if not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
                return UNKNOWN
            return _marker_mapping_tags(
                {k.value for k in node.keys},
                any(isinstance(v, (ast.Constant, ast.Tuple, ast.List)) for v in node.values),
            )
        if isinstance(node, ast.DictComp) and len(node.generators) == 1:
            generator = node.generators[0]
            if generator.ifs or generator.is_async or not isinstance(generator.target, ast.Name):
                return UNKNOWN
            try:
                keys = ast.literal_eval(generator.iter)
            except (ValueError, TypeError):
                return UNKNOWN
            if not isinstance(keys, (tuple, list, set)) or not all(isinstance(k, str) for k in keys):
                return UNKNOWN
            if isinstance(node.key, ast.Name) and node.key.id == generator.target.id:
                return _marker_mapping_tags(keys, isinstance(node.value, (ast.Constant, ast.Tuple, ast.List)))
            return UNKNOWN
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args:
                if any(k.arg is None for k in node.keywords):
                    return UNKNOWN
                return _marker_mapping_tags(
                    {k.arg for k in node.keywords},
                    any(isinstance(k.value, (ast.Constant, ast.Tuple, ast.List)) for k in node.keywords),
                )
            if (
                (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "dumps"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in json_modules
                )
                or isinstance(node.func, ast.Name)
                and node.func.id in json_functions
            ):
                return frozenset({"json_text"})
            if isinstance(node.func, ast.Name):
                if node.func.id in ("Vector", "tuple", "list"):
                    return frozenset({"vector" if node.func.id == "Vector" else "tuple"})
                if node.func.id in TUPLE_HELPERS:
                    return frozenset({"tuple"})
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                "copy",
                "normalized",
                "lerp",
            ):
                return infer(node.func.value, env)
        if isinstance(node, ast.Attribute) and node.attr in ("location", "translation", "scale"):
            return frozenset({"vector"})
        if isinstance(node, ast.IfExp):
            return infer(node.body, env) | infer(node.orelse, env)
        if isinstance(node, ast.BinOp):
            left, right = infer(node.left, env), infer(node.right, env)
            if left == frozenset({"vector"}) or right == frozenset({"vector"}):
                return frozenset({"vector"})
        return UNKNOWN

    api_functions = json.loads(previs_api_contract())["functions"]
    api_parameters = {
        name: ast.parse("def " + spec["signature"] + ": pass").body[0].args
        for name, spec in api_functions.items()
    }

    def expression(node, env):
        if node is None:
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in api_functions:
            name = node.func.id
            parameters = api_parameters[name]
            positional = [*parameters.posonlyargs, *parameters.args]
            supplied = {arg.arg for arg in positional[:len(node.args)]}
            supplied.update(k.arg for k in node.keywords if k.arg is not None)
            supported = {arg.arg for arg in [*positional, *parameters.kwonlyargs]}
            expanded = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
            missing = set(api_functions[name]["requiredArguments"]) - supplied if not expanded else set()
            unsupported = {k.arg for k in node.keywords if k.arg is not None} - supported
            too_many = not expanded and parameters.vararg is None and len(node.args) > len(positional)
            if missing or unsupported or too_many:
                issues.append({
                    "code": "PREVIS_API_ARGUMENTS", "line": node.lineno,
                    "message": "Use the supplied helper contract. Missing arguments: " + ", ".join(sorted(missing))
                    + "; unsupported arguments: " + ", ".join(sorted(unsupported))
                    + ("; too many positional arguments" if too_many else "")
                    + ". Contract: " + json.dumps(api_functions[name], ensure_ascii=False),
                })
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "evaluated_depsgraph_get"
            and (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id in scene_names
                or isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "scene"
            )
        ):
            issues.append(
                {
                    "code": "BLENDER_DEPSGRAPH_API",
                    "line": node.lineno,
                    "message": "Scene has no evaluated_depsgraph_get(). Use bpy.context.evaluated_depsgraph_get() after scene.frame_set(frame).",
                }
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "previs_camera_register_subjects"
        ):
            invalid = sorted(
                {
                    keyword.arg
                    for keyword in node.keywords
                    if keyword.arg is not None and keyword.arg not in {"camera", "subjects"}
                }
            )
            if invalid or len(node.args) > 2:
                issues.append(
                    {
                        "code": "CAMERA_SUBJECTS_SIGNATURE",
                        "line": node.lineno,
                        "message": "Use previs_camera_register_subjects(camera, subjects) with exactly these two parameters. "
                        "required_markers belongs only to previs_register_motion(root, markers, required_markers=...). "
                        "Unsupported keywords: " + ", ".join(invalid),
                    }
                )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            expected = {
                int(t.split(":")[1])
                for t in infer(node.left, env)
                if t.startswith("percent_arity:")
            }
            supplied = {
                int(t.split(":")[1]) for t in infer(node.right, env) if t.startswith("tuple_arity:")
            }
            if len(expected) == len(supplied) == 1 and expected != supplied:
                issues.append(
                    {
                        "code": "STRING_PERCENT_FORMAT",
                        "line": node.lineno,
                        "message": f"String format expects {next(iter(expected))} values but tuple has {next(iter(supplied))}. "
                        + JSON_LOG_POLICY,
                    }
                )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            if len(node.args) != 1 or infer(node.args[0], env) != frozenset({"json_text"}):
                issues.append(
                    {"code": "JSON_LOG_REQUIRED", "line": node.lineno, "message": JSON_LOG_POLICY}
                )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            if node.func.attr == "update":
                previous = env.get(node.func.value.id, UNKNOWN)
                incoming = infer(node.args[0], env) if len(node.args) == 1 and not node.keywords else UNKNOWN
                if "mapping_keys_known" in previous and "mapping_keys_known" in incoming:
                    keys = {tag.removeprefix("marker_key:") for tag in previous | incoming
                            if tag.startswith("marker_key:")}
                    env[node.func.value.id] = _marker_mapping_tags(
                        keys, "marker_values_invalid" in previous | incoming,
                    )
                else:
                    env[node.func.value.id] = UNKNOWN
            elif node.func.attr in ("pop", "clear", "setdefault", "__setitem__"):
                env[node.func.value.id] = UNKNOWN
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "previs_register_motion"
        ):
            markers = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "markers"), None)
            )
            types = infer(markers, env)
            if (
                "tuple" in types
                or "scalar" in types
                or (
                    "unknown" not in types
                    and types.intersection({"marker_keys_invalid", "marker_values_invalid"})
                )
            ):
                issues.append(
                    {
                        "code": "MOTION_MARKERS",
                        "line": node.lineno,
                        "message": MOTION_MARKER_CONTRACT,
                    }
                )
        if isinstance(node, ast.Attribute) and node.attr in VECTOR_ATTRIBUTES:
            if "tuple" in infer(node.value, env):
                issues.append(
                    {
                        "code": "VECTOR_TYPE",
                        "line": node.lineno,
                        "message": f"{ast.unparse(node.value)} may be tuple/list before .{node.attr}. Convert it to mathutils.Vector at assignment before vector operations.",
                    }
                )
        for child in ast.iter_child_nodes(node):
            expression(child, env)

    def assign(target, value, env):
        if isinstance(target, ast.Name):
            env[target.id] = infer(value, env)
        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            env[target.value.id] = UNKNOWN
        elif isinstance(target, (ast.Tuple, ast.List)):
            if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
                for dest, source in zip(target.elts, value.elts):
                    assign(dest, source, env)
            else:
                for dest in target.elts:
                    if isinstance(dest, ast.Name):
                        two_bone = (
                            isinstance(value, ast.Call)
                            and isinstance(value.func, ast.Name)
                            and value.func.id == "_pv_two_bone"
                        )
                        env[dest.id] = frozenset({"tuple"}) if two_bone else UNKNOWN

    def merge(left, right):
        return {
            name: left.get(name, UNKNOWN) | right.get(name, UNKNOWN)
            for name in left.keys() | right.keys()
        }

    def statements(body, env):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local = dict(env)
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    local[arg.arg] = UNKNOWN
                statements(node.body, local)
            elif isinstance(node, ast.ClassDef):
                statements(node.body, dict(env))
            elif isinstance(node, ast.If):
                expression(node.test, env)
                env = merge(statements(node.body, dict(env)), statements(node.orelse, dict(env)))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                expression(node.iter if hasattr(node, "iter") else node.test, env)
                loop = dict(env)
                if hasattr(node, "target"):
                    if isinstance(node.iter, (ast.List, ast.Tuple)) and node.iter.elts:
                        choices = []
                        for item in node.iter.elts:
                            current = dict(env)
                            assign(node.target, item, current)
                            choices.append(current)
                        for current in choices:
                            loop = merge(loop, current)
                    else:
                        assign(node.target, ast.Constant(None), loop)
                env = merge(env, statements(node.body, loop))
                env = statements(node.orelse, env)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    expression(item.context_expr, env)
                env = statements(node.body, env)
            elif isinstance(node, ast.Try):
                variants = [statements(node.body, dict(env))]
                variants.extend(statements(handler.body, dict(env)) for handler in node.handlers)
                for variant in variants:
                    env = merge(env, variant)
                env = statements(node.orelse, env)
                env = statements(node.finalbody, env)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                expression(node.value, env)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    expression(target, env)
                    assign(target, node.value, env)
            else:
                expression(node, env)
        return env

    statements(tree.body, {})
    unique = {(issue["code"], issue["line"], issue["message"]): issue for issue in issues}
    return list(unique.values())[:20]
