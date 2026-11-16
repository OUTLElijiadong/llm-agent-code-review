#!/usr/bin/env python3
"""Probe the Xiaoling F2 authority contract in a bound backend image."""

from __future__ import annotations

import ast
import os
import pathlib
import signal
import sys


class ProbeTimeout(Exception):
    pass


def _timeout(_signum, _frame):
    raise ProbeTimeout


def _module(root: pathlib.Path, relative: str) -> ast.Module:
    return ast.parse((root / relative).read_text(encoding="utf-8"))


def _function(module: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise ValueError(f"missing function: {name}")


def _method(owner: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in owner.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise ValueError(f"missing method: {owner.name}.{name}")


def _calls(function: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        for node in ast.walk(function)
    )


def _has_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


def _has_attribute(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Attribute) and child.attr == name for child in ast.walk(node))


def _has_attribute_assignment_from_call(function: ast.AST, attribute: str, function_name: str) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == attribute for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == function_name
        for node in ast.walk(function)
    )


def _has_nullable_bigint_column(owner: ast.ClassDef, name: str) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "Column"
        and node.value.args
        and isinstance(node.value.args[0], ast.Name)
        and node.value.args[0].id == "BigInteger"
        and any(keyword.arg == "nullable" and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True for keyword in node.value.keywords)
        for node in owner.body
    )


def _has_string(node: ast.AST, value: str) -> bool:
    return any(isinstance(child, ast.Constant) and child.value == value for child in ast.walk(node))


def _has_compare(node: ast.AST, *, left: str, operator: type[ast.cmpop], right: str | None = None) -> bool:
    return any(
        isinstance(child, ast.Compare)
        and isinstance(child.left, ast.Name)
        and child.left.id == left
        and len(child.ops) == 1
        and isinstance(child.ops[0], operator)
        and (right is None or any(isinstance(item, ast.Name) and item.id == right
                                  for comparator in child.comparators for item in ast.walk(comparator)))
        for child in ast.walk(node)
    )


def _has_raise(function: ast.AST, exception_name: str) -> bool:
    return any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == exception_name
        for node in ast.walk(function)
    )


def _has_len_greater_than(function: ast.AST, collection: str, limit: str) -> bool:
    return any(
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == "len"
        and len(node.left.args) == 1
        and isinstance(node.left.args[0], ast.Name)
        and node.left.args[0].id == collection
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Gt)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id == limit
        for node in ast.walk(function)
    )


def _has_limit_plus_one(function: ast.AST, constant: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "limit"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.BinOp)
        and isinstance(node.args[0].op, ast.Add)
        and isinstance(node.args[0].left, ast.Name)
        and node.args[0].left.id == constant
        and isinstance(node.args[0].right, ast.Constant)
        and node.args[0].right.value == 1
        for node in ast.walk(function)
    )


def _is_len_call(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == name
    )


def _is_context_budget_condition(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.BinOp)
        and isinstance(node.left.op, ast.Add)
        and _is_len_call(node.left.left, "prefix")
        and _is_len_call(node.left.right, "suffix")
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Gt)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Attribute)
        and node.comparators[0].attr == "_MAX_CONTEXT_CHARS"
    )


def _directly_raises(statements: list[ast.stmt], exception_name: str) -> bool:
    return any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == exception_name
        for node in statements
    )


def _context_budget_guard_index(function: ast.AST) -> int | None:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    for index, node in enumerate(function.body):
        if (isinstance(node, ast.If) and _is_context_budget_condition(node.test)
                and _directly_raises(node.body, "ValidationError")):
            return index
    return None


def _has_context_budget_guard(function: ast.AST) -> bool:
    return _context_budget_guard_index(function) is not None


def _has_recent_context_budget_loop(function: ast.AST) -> bool:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    guard_index = _context_budget_guard_index(function)
    if guard_index is None:
        return False
    for loop_index, node in enumerate(function.body):
        if not isinstance(node, ast.For):
            continue
        if not (
            isinstance(node.target, ast.Name)
            and node.target.id == "item"
            and isinstance(node.iter, ast.Call)
            and isinstance(node.iter.func, ast.Name)
            and node.iter.func.id == "reversed"
            and len(node.iter.args) == 1
            and isinstance(node.iter.args[0], ast.Name)
            and node.iter.args[0].id == "rendered"
        ):
            continue
        limit_guard = next((item for item in node.body
                            if isinstance(item, ast.If) and ast.unparse(item.test) == "cost > budget"), None)
        if limit_guard is None or not any(isinstance(item, ast.Break) for item in limit_guard.body):
            continue
        limit_guard_index = node.body.index(limit_guard)
        append_index = next((index for index, item in enumerate(node.body)
                             if isinstance(item, ast.Expr) and isinstance(item.value, ast.Call)
                             and isinstance(item.value.func, ast.Attribute) and item.value.func.attr == "append"
                             and isinstance(item.value.func.value, ast.Name)
                             and item.value.func.value.id == "recent"
                             and len(item.value.args) == 1 and isinstance(item.value.args[0], ast.Name)
                             and item.value.args[0].id == "item"), None)
        budget_decrement_index = next((index for index, item in enumerate(node.body)
                                       if isinstance(item, ast.AugAssign)
                                       and isinstance(item.target, ast.Name) and item.target.id == "budget"
                                       and isinstance(item.op, ast.Sub)
                                       and isinstance(item.value, ast.Name) and item.value.id == "cost"), None)
        has_append = append_index is not None
        has_budget_decrement = budget_decrement_index is not None
        has_cost_assignment = any(
            isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "cost"
                                                 for target in item.targets)
            and _has_name(item.value, "item") and _has_name(item.value, "recent")
            for item in node.body[:node.body.index(limit_guard)]
        )
        budget_index = next((index for index, item in enumerate(function.body)
                             if isinstance(item, ast.Assign)
                             and any(isinstance(target, ast.Name) and target.id == "budget"
                                     for target in item.targets)
                             and _has_attribute(item.value, "_MAX_CONTEXT_CHARS")
                             and _has_name(item.value, "prefix") and _has_name(item.value, "suffix")), None)
        prompt_index = next((index for index, item in enumerate(function.body)
                             if isinstance(item, ast.Assign)
                             and any(isinstance(target, ast.Name) and target.id == "prompt"
                                     for target in item.targets)
                             and _has_name(item.value, "prefix") and _has_name(item.value, "recent")
                             and _has_name(item.value, "suffix") and _has_attribute(item.value, "join")), None)
        if (has_append and has_budget_decrement and limit_guard_index < append_index
                and limit_guard_index < budget_decrement_index
                and has_cost_assignment and budget_index is not None
                and prompt_index is not None and guard_index < budget_index < loop_index < prompt_index):
            return True
    return False


def _has_finalizer_clearance_before_publication(function: ast.AST) -> bool:
    def withholds_after_exception(handler: ast.ExceptHandler) -> bool:
        writes = [
            node for node in ast.walk(handler)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "withheld" for target in node.targets)
        ]
        return (
            len(writes) == 1
            and writes[0] in handler.body
            and isinstance(writes[0].value, ast.Constant)
            and writes[0].value.value is True
        )

    for parent in ast.walk(function):
        for _field, value in ast.iter_fields(parent):
            if not isinstance(value, list):
                continue
            for index, statement in enumerate(value):
                if not isinstance(statement, ast.Try):
                    continue
                direct_clearance = any(
                    isinstance(item, ast.Expr)
                    and isinstance(item.value, ast.Call)
                    and isinstance(item.value.func, ast.Name)
                    and item.value.func.id == "_require_model_send_clearance"
                    for item in statement.body
                )
                clearance_index = next((index for index, item in enumerate(statement.body)
                                         if isinstance(item, ast.Expr)
                                         and isinstance(item.value, ast.Call)
                                         and isinstance(item.value.func, ast.Name)
                                         and item.value.func.id == "_require_model_send_clearance"), None)
                publishes_before_clearance = (
                    clearance_index is None
                    or any(
                        _calls(item, "_ensure_message")
                        or any(isinstance(node, ast.Assign)
                               and any(isinstance(target, ast.Attribute) and target.attr == "response_text"
                                       for target in node.targets)
                               for node in ast.walk(item))
                        for item in statement.body[:clearance_index]
                    )
                )
                handles_superseded = any(
                    handler.type is not None
                    and _has_name(handler.type, "SupersededByAuthorityDirectiveError")
                    and withholds_after_exception(handler)
                    for handler in statement.handlers
                )
                handles_forbidden = any(
                    handler.type is not None and _has_name(handler.type, "ForbiddenError")
                    and withholds_after_exception(handler)
                    for handler in statement.handlers
                )
                if not (direct_clearance and not publishes_before_clearance
                        and handles_superseded and handles_forbidden):
                    continue
                if index + 1 >= len(value):
                    continue
                following = value[index + 1]
                if not isinstance(following, ast.If) or ast.unparse(following.test) != "not withheld":
                    continue
                publishes_response = any(
                    isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Attribute) and target.attr == "response_text"
                            for target in node.targets)
                    for node in following.body
                )
                if publishes_response:
                    return True
    return False


def _integer_constant(module: ast.Module, name: str, expected: int) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and node.value.value == expected
        for node in module.body
    )


def _probe(root: pathlib.Path) -> bool:
    chat = _module(root, "services/enterprise_chat_service.py")
    send = _function(chat, "send_message")
    rank_write = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "EnterpriseChatMessage"
        and any(keyword.arg == "sender_authority_rank" and _has_name(keyword.value, "authority_rank")
                for keyword in node.keywords)
        for node in ast.walk(send)
    )
    if not rank_write:
        return False

    runtime = _module(root, "services/enterprise_assistant_runtime_service.py")
    chat_models = _module(root, "models/enterprise_chat.py")
    message_model = next(
        (node for node in chat_models.body if isinstance(node, ast.ClassDef)
         and node.name == "EnterpriseChatMessage"),
        None,
    )
    if message_model is None or not _has_nullable_bigint_column(
        message_model, "discussion_closed_through_message_id",
    ):
        return False
    if (not _integer_constant(runtime, "_MAX_DISCUSSION_MESSAGES", 2000)
            or not _integer_constant(runtime, "_MAX_AUTHORITY_CANDIDATES", 64)):
        return False
    completed_boundary = _function(runtime, "_last_completed_assistant_discussion_cutoff")
    boundary_terms = ast.unparse(completed_boundary)
    if ("EnterpriseAssistantRun.status == 'completed'" not in boundary_terms
            or "EnterpriseAssistantRun.response_text != ''" not in boundary_terms
            or "EnterpriseChatMessage.sender_type == 'assistant'" not in boundary_terms
            or "discussion_closed_through_message_id" not in boundary_terms):
        return False

    discussion = _function(runtime, "_current_discussion_messages")
    if (not _calls(discussion, "_last_completed_assistant_discussion_cutoff")
            or not _has_limit_plus_one(discussion, "_MAX_DISCUSSION_MESSAGES")
            or not _has_len_greater_than(discussion, "messages", "_MAX_DISCUSSION_MESSAGES")
            or not _has_raise(discussion, "ValidationError")):
        return False

    publisher = _function(runtime, "_latest_human_message_id")
    if ("EnterpriseChatMessage.sender_type == 'human'" not in ast.unparse(publisher)
            or not _has_attribute(publisher, "with_for_update")):
        return False

    candidates = _function(runtime, "_authority_candidate_messages")
    if (not _calls(candidates, "_message_authority_rank")
            or not _has_compare(candidates, left="rank", operator=ast.GtE, right="initiator_rank")
            or not _has_string(candidates, "@小菱")
            or not _has_attribute(candidates, "sender_authority_rank")
            or not _has_len_greater_than(candidates, "candidates", "_MAX_AUTHORITY_CANDIDATES")
            or not _has_raise(candidates, "ValidationError")):
        return False

    newer = _function(runtime, "_newer_human_messages")
    if (not _has_limit_plus_one(newer, "_MAX_DISCUSSION_MESSAGES")
            or not _has_len_greater_than(newer, "messages", "_MAX_DISCUSSION_MESSAGES")
            or not _has_raise(newer, "ForbiddenError")
    ):
        return False

    live_snapshot = _function(runtime, "_live_role_snapshot")
    newer_guard = _function(runtime, "_assert_no_newer_authority_directive")
    if (not _calls(live_snapshot, "_assert_no_newer_authority_directive")
            or not _calls(newer_guard, "_newer_human_messages")):
        return False

    render_context = _function(runtime, "_render_runtime_context")
    if (not _calls(render_context, "_authority_candidate_messages")
            or not _calls(render_context, "_message_authority_rank")
            or not _has_name(render_context, "authority_context")
            or not _has_context_budget_guard(render_context)
            or not _has_recent_context_budget_loop(render_context)
            or not _has_raise(render_context, "ValidationError")):
        return False

    billing_scope = _function(runtime, "_billing_scope")
    if not _calls(billing_scope, "_require_model_send_clearance"):
        return False
    tool_executor = next(
        (node for node in runtime.body if isinstance(node, ast.ClassDef)
         and node.name == "EnterpriseAssistantToolExecutor"),
        None,
    )
    if tool_executor is None or not _calls(_method(tool_executor, "_current"), "_live_role_snapshot"):
        return False

    finalize = _function(runtime, "_finalize_runtime")
    if (not _has_finalizer_clearance_before_publication(finalize)
            or not _calls(finalize, "_latest_human_message_id")
            or not _has_attribute_assignment_from_call(
                finalize, "discussion_closed_through_message_id", "_latest_human_message_id",
            )):
        return False

    clarification = _module(root, "services/enterprise_assistant_clarification_service.py")
    context = _function(clarification, "_context")
    return (_calls(context, "_current_discussion_messages")
            and _calls(context, "_authority_candidate_messages")
            and _calls(context, "_message_authority_rank")
            and _calls(context, "_newer_human_messages"))


def main() -> int:
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    root = pathlib.Path(os.environ.get("PRISM_APP_ROOT", "/app/app"))
    try:
        supported = _probe(root)
    except (OSError, SyntaxError, ValueError, ProbeTimeout):
        supported = False
    finally:
        signal.alarm(0)
    print("supported" if supported else "unsupported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
