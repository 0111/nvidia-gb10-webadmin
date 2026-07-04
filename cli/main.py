"""CLI entry point: start/stop/restart/clean/model_check.

Every subcommand first runs the env_doctor checklist (cuda-compat -> NIC
speed -> drop_caches -> swap) and prints the report, matching the flow in
Project_Task.md 2.3.2. Any "fix" is only ever applied after an interactive
y/N confirmation — this CLI never silently performs sudo/apt/swapoff.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import env_doctor, process_manager
from core.config import AppConfig, DEFAULT_CONFIG_PATH, load_config
from core.docker_helper import DockerComposeManager
from core.model_scanner import list_embedding_models, list_general_models, scan_models

STATUS_ICON = {"ok": "[OK]", "warning": "[WARN]", "error": "[ERROR]"}

FIX_FUNCS = {
    "cuda_compat": lambda confirmed: env_doctor.fix_cuda_compat(confirmed=confirmed),
    "ethernet_speed": None,  # needs interface name, handled inline
    "drop_caches": lambda confirmed: env_doctor.fix_drop_caches(confirmed=confirmed),
    "swap": lambda confirmed: env_doctor.fix_swap(confirmed=confirmed),
}


def _print_check(check: env_doctor.CheckResult) -> None:
    icon = STATUS_ICON.get(check.status, "[?]")
    click.echo(f"  {icon} [{check.name}] {check.message}")
    if check.suggested_command:
        click.echo(f"      建议命令: {check.suggested_command}")


def run_env_checklist(auto_fix_prompt: bool = True) -> env_doctor.EnvReport:
    """Run all env checks, print results, optionally prompt to fix issues."""
    click.echo("== 环境检测 ==")
    report = env_doctor.run_all_checks()
    for check in report.checks:
        _print_check(check)

    if not auto_fix_prompt:
        return report

    for check in report.checks:
        if check.status == "ok" or not check.fixable:
            continue
        if not click.confirm(f"是否立即执行 [{check.name}] 的修复命令？", default=False):
            continue

        if check.name == "ethernet_speed":
            interface = check.details.get("interface")
            if not interface:
                click.echo("  无法确定网卡接口名，跳过")
                continue
            result = env_doctor.fix_ethernet_speed(interface, confirmed=True)
        else:
            fix_fn = FIX_FUNCS.get(check.name)
            if fix_fn is None:
                continue
            result = fix_fn(True)

        click.echo(f"  执行结果: {result.message} (rc={result.returncode})")
        if result.stderr:
            click.echo(f"  stderr: {result.stderr.strip()}")

    return report


@click.group()
@click.option("--config", "config_path", default=str(DEFAULT_CONFIG_PATH),
              help="配置文件路径")
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """NVIDIA DGX Spark / GB10 本地大模型管理工具 CLI"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["config"] = load_config(config_path)


def _print_runtime_summary(config: AppConfig) -> None:
    click.echo("== 运行信息 ==")
    click.echo(f"  Web 端口: {config.web_port}")
    click.echo(f"  API Base URL: http://{config.web_host}:{config.web_port}")
    click.echo(f"  前端访问地址: http://{config.web_host}:{config.frontend_port}")
    click.echo(f"  管理员账号: {config.admin_username}")
    click.echo(f"  管理员密码: {config.admin_password}")
    click.echo(f"  密钥(secret_key): {config.secret_key}")
    click.echo(f"  模型根目录: {config.model_root_dir}")
    click.echo(f"  数据目录: {config.data_dir}")


def _print_component_results(results: list[dict]) -> bool:
    """Print a list of {component, ok, message} dicts; returns overall ok."""
    overall_ok = True
    for item in results:
        icon = "[OK]" if item["ok"] else "[FAIL]"
        if not item["ok"]:
            overall_ok = False
        click.echo(f"  {icon} {item['component']}: {item['message']}")
    return overall_ok


@cli.command()
@click.pass_context
def start(ctx: click.Context) -> None:
    """启动整个项目组件（含 Web 系统）"""
    config: AppConfig = ctx.obj["config"]
    try:
        run_env_checklist()
        click.echo("== 启动组件 ==")
        results = process_manager.start_all(config)
        ok = _print_component_results(results)
        if not ok:
            click.echo("  部分组件启动失败，请检查上方错误信息", err=True)
        _print_runtime_summary(config)
    except Exception as exc:  # pragma: no cover - defensive, CLI must never crash
        click.echo(f"[ERROR] start 命令执行异常: {exc}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def stop(ctx: click.Context) -> None:
    """停止整个项目组件（不删除容器，对应 docker compose stop）"""
    config: AppConfig = ctx.obj["config"]
    try:
        run_env_checklist(auto_fix_prompt=False)
        click.echo("== 停止组件 ==")
        results = process_manager.stop_all(config)
        ok = _print_component_results(results)
        if not ok:
            click.echo("  部分组件停止失败，请检查上方错误信息", err=True)
    except Exception as exc:  # pragma: no cover - defensive
        click.echo(f"[ERROR] stop 命令执行异常: {exc}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def restart(ctx: click.Context) -> None:
    """重启整个项目组件（等价于先 stop 再 start）"""
    config: AppConfig = ctx.obj["config"]
    try:
        click.echo("== 重启：第一步 stop ==")
        stop_results = process_manager.stop_all(config)
        _print_component_results(stop_results)

        run_env_checklist()
        click.echo("== 重启：第二步 start ==")
        start_results = process_manager.start_all(config)
        ok = _print_component_results(start_results)
        if not ok:
            click.echo("  部分组件启动失败，请检查上方错误信息", err=True)
        _print_runtime_summary(config)
    except Exception as exc:  # pragma: no cover - defensive
        click.echo(f"[ERROR] restart 命令执行异常: {exc}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def clean(ctx: click.Context) -> None:
    """清理项目运行产生的容器/网络/web.pid（不清理模型扫描结果、性能报告、配置文件等持久化数据）"""
    config: AppConfig = ctx.obj["config"]
    try:
        run_env_checklist(auto_fix_prompt=False)
        click.echo("== 清理 ==")
        if not click.confirm("将停止并删除所有项目容器与网络（不影响模型扫描结果/性能报告/配置文件），是否继续？", default=False):
            click.echo("  已取消")
            return
        results = process_manager.clean_all(config)
        ok = _print_component_results(results)
        if not ok:
            click.echo("  部分清理步骤失败，请检查上方错误信息", err=True)
        click.echo("  保留的持久化数据：model_scan_result.json / data/reports/ / config/settings.yaml")
    except Exception as exc:  # pragma: no cover - defensive
        click.echo(f"[ERROR] clean 命令执行异常: {exc}", err=True)
        sys.exit(1)


@cli.command(name="model_check")
@click.option("--verify-hash", is_flag=True, default=False,
              help="对带有哈希清单(SHA256SUMS/*.sha256)的模型做SHA256深度校验（读取完整文件，较慢）")
@click.pass_context
def model_check(ctx: click.Context, verify_hash: bool) -> None:
    """扫描本地模型目录，多维度校验文件完整性并落盘结果供 Web 后端读取。

    默认做静态校验（格式/后缀、config.json解析、权重清单index.json核对、
    分片完整性、空文件/截断、total_size大小核对）。加 --verify-hash 时，
    对附带哈希清单的模型额外做 SHA256 深度校验。
    """
    config: AppConfig = ctx.obj["config"]
    try:
        run_env_checklist(auto_fix_prompt=False)

        click.echo("== 模型扫描 ==")
        models = scan_models(config.model_root_dir)
        general = list_general_models(models)
        embedding = list_embedding_models(models)

        invalid = [m for m in models if not m.valid]
        click.echo(f"  共发现 {len(models)} 个模型 (通用 {len(general)} / Embedding {len(embedding)})"
                    f"，其中 {len(invalid)} 个文件校验未通过")
        for m in models:
            flag = "[Embedding]" if m.is_embedding else "[General]  "
            verdict = STATUS_ICON["ok"] if m.valid else STATUS_ICON["error"]
            warn = f"  WARNINGS: {'; '.join(m.warnings)}" if m.warnings else ""
            click.echo(f"  {verdict} {flag} {m.name:40s} fmt={m.format:12s} engine={m.engine_hint:8s} "
                        f"quant={m.quantization or '-':10s} ctx={m.max_position_embeddings or '-'}{warn}")
            if not m.valid:
                click.echo(f"        [校验失败] {'; '.join(m.validation_errors)}")

        if verify_hash:
            from core.model_scanner import verify_model_hashes

            click.echo("== SHA256 深度校验（仅对带哈希清单的模型）==")
            any_manifest = False
            for m in models:
                result = verify_model_hashes(m.path)
                if not result["available"]:
                    continue
                any_manifest = True
                icon = STATUS_ICON["ok"] if result["ok"] else STATUS_ICON["error"]
                click.echo(f"  {icon} {m.name}")
                for f in result["files"]:
                    fi = STATUS_ICON["ok"] if f["ok"] else STATUS_ICON["error"]
                    click.echo(f"        {fi} {f['file']}: {f['detail']}")
            if not any_manifest:
                click.echo("  未发现任何附带 SHA256 哈希清单的模型，跳过哈希校验。")

        # Persist via the shared helper so the format (incl. scanned_at) matches
        # exactly what the Web backend's model_cache loads — a CLI scan thus
        # transparently refreshes what the running web UI shows.
        from core.model_scanner import save_scan_result, scan_result_path
        save_scan_result(models, config.data_dir, config.model_root_dir)
        click.echo(f"  扫描结果已写入: {scan_result_path(config.data_dir)}")
    except Exception as exc:  # pragma: no cover - defensive
        click.echo(f"[ERROR] model_check 命令执行异常: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli(obj={})
