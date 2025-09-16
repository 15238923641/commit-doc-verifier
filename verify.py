#!/usr/bin/env python3
# =============================================================================
# GitHub功能提交跟踪验证脚本
# =============================================================================
import sys
import os
import requests
import argparse
import yaml
import re
import base64
import json
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================
# 配置常量
# ==========================
DEFAULT_ENV_FILE = ".env"
DEFAULT_CONFIG_FILE = "config.yaml"
GITHUB_API_VERSION = "application/vnd.github.v3+json"

# ==========================
# 工具函数
# ==========================
def create_session_with_retry():
    """创建带重试机制的请求会话"""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

def load_environment(env_path: str) -> Tuple[str, str]:
    """
    加载环境变量（从.env文件）
    返回：(GitHub Token, GitHub 组织名)
    """
    if not os.path.exists(env_path):
        print(f"❌ 错误：环境文件 {env_path} 不存在", file=sys.stderr)
        sys.exit(1)

    load_dotenv(env_path)
    github_token = os.getenv("GITHUB_TOKEN")
    github_org = os.getenv("GITHUB_ORG")

    if not github_token:
        print(f"❌ 错误：{env_path}文件中未配置 GITHUB_TOKEN", file=sys.stderr)
        sys.exit(1)
    if not github_org:
        print(f"❌ 错误：{env_path}文件中未配置 GITHUB_ORG", file=sys.stderr)
        sys.exit(1)

    return github_token, github_org

def load_project_config(config_path: str) -> Dict:
    """
    加载项目配置（从YAML文件）
    配置文件需包含：文档路径、验证规则、预期数据等
    """
    if not os.path.exists(config_path):
        print(f"❌ 错误：配置文件 {config_path} 不存在", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        # 验证配置完整性
        required_config_fields = [
            "target_repo",
            "target_branch", 
            "feature_doc_path",
            "table_header",
            "required_sections",
            "min_feature_count",
            "expected_features",
            "expected_authors",
            "expected_messages", 
            "expected_dates"
        ]

        for field in required_config_fields:
            if field not in config:
                print(f"❌ 错误：配置文件缺少必填字段「{field}」", file=sys.stderr)
                sys.exit(1)
        
        return config

    except Exception as e:
        print(f"❌ 错误：加载配置文件失败 - {str(e)}", file=sys.stderr)
        sys.exit(1)

def get_github_headers(token: str) -> Dict[str, str]:
    """生成GitHub API请求头"""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": GITHUB_API_VERSION,
        "User-Agent": "GitHub-Commit-Verifier/1.0"
    }

def fetch_github_file(
    file_path: str,
    headers: Dict[str, str],
    org: str,
    repo: str,
    branch: str
) -> Optional[str]:
    """
    从GitHub仓库获取文件内容（自动解码Base64）
    返回：文件内容（字符串）或None（失败）
    """
    api_url = f"https://api.github.com/repos/{org}/{repo}/contents/{file_path}?ref={branch}"
    
    session = create_session_with_retry()
    
    try:
        response = session.get(api_url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("encoding") == "base64":
                return base64.b64decode(data["content"]).decode("utf-8")
            return data.get("content", "")
        
        elif response.status_code == 404:
            print(f"❌ 错误：文件 {file_path} 在 {branch} 分支不存在", file=sys.stderr)
            return None
        elif response.status_code == 403:
            print(f"❌ 错误：API访问受限（可能达到速率限制）", file=sys.stderr)
            return None
        else:
            print(f"❌ 错误：获取文件失败（状态码：{response.status_code}）", file=sys.stderr)
            print(f"   响应：{response.text[:200]}", file=sys.stderr)
            return None

    except requests.Timeout:
        print(f"❌ 错误：请求超时", file=sys.stderr)
        return None
    except Exception as e:
        print(f"❌ 错误：请求GitHub API异常 - {str(e)}", file=sys.stderr)
        return None

def verify_commit(
    commit_sha: str,
    headers: Dict[str, str],
    org: str,
    repo: str
) -> Optional[Dict]:
    """
    验证GitHub提交是否存在，并返回提交详情
    返回：提交详情（字典）或None（失败）
    """
    # 前置校验：SHA格式检查
    if not re.match(r'^[a-f0-9]{40}$', commit_sha):
        print(f"❌ 错误：无效的SHA格式 {commit_sha}（必须为40位小写十六进制）", file=sys.stderr)
        return None

    api_url = f"https://api.github.com/repos/{org}/{repo}/commits/{commit_sha}"
    session = create_session_with_retry()
    
    try:
        response = session.get(api_url, headers=headers, timeout=30)
        
        # 处理HTTP错误状态码
        if response.status_code != 200:
            error_msg = f"❌ GitHub API 错误（状态码：{response.status_code}）"
            if response.status_code == 404:
                error_msg += f"\n   提交 {commit_sha[:8]} 不存在于 {org}/{repo}"
            elif response.status_code == 403:
                rate_limit = response.headers.get('X-RateLimit-Remaining', '?')
                error_msg += f"\n   API速率限制剩余：{rate_limit}"
            print(error_msg, file=sys.stderr)
            return None

        commit_detail = response.json()
        
        # 关键字段存在性检查
        required_fields = ['sha', 'commit']
        for field in required_fields:
            if field not in commit_detail:
                print(f"❌ 错误：提交详情缺少关键字段 '{field}'", file=sys.stderr)
                return None
        
        # 处理author字段可能为null的情况
        if 'author' in commit_detail and commit_detail['author'] is not None:
            if not isinstance(commit_detail['author'], dict):
                print(f"❌ 错误：提交详情的author字段不是字典格式", file=sys.stderr)
                return None
                
        return commit_detail

    except requests.Timeout:
        print(f"❌ 错误：请求提交详情超时（30秒）", file=sys.stderr)
        return None
        
    except json.JSONDecodeError:
        print(f"❌ 错误：无法解析API返回的JSON数据", file=sys.stderr)
        return None
        
    except Exception as e:
        print(f"❌ 未捕获的异常：{type(e).__name__} - {str(e)}", file=sys.stderr)
        return None

# ==========================
# 核心逻辑
# ==========================
def parse_feature_table(content: str, table_header: str) -> List[Dict]:
    """
    解析Markdown文档中的特征表格
    返回：解析后的特征列表（每个元素是特征字典）
    """
    features = []
    lines = content.split("\n")
    table_started = False
    header_found = False

    for line in lines:
        line = line.strip()
        
        # 找到表格开始（表头行）
        if table_header in line:
            table_started = True
            header_found = True
            continue
            
        if not table_started:
            continue
            
        # 表格结束条件（遇到新的章节标题）
        if line.startswith("##") and table_started:
            break
            
        # 跳过表格分隔线
        if line.startswith("|") and "---" in line:
            continue
            
        # 解析表格行
        if line.startswith("|") and table_started:
            # 移除首尾的|并分割
            cells = [cell.strip() for cell in line.split("|")[1:-1]]
            if len(cells) >= 7:
                feature = {
                    "name": cells[0],
                    "sha": cells[1],
                    "author": cells[2],
                    "branch": cells[3],
                    "date": cells[4],
                    "files_changed": cells[5],
                    "message": cells[6]
                }
                features.append(feature)
    
    return features

def run_verification(config: Dict, github_token: str, github_org: str) -> bool:
    """
    执行完整验证流程
    返回：True（验证通过）/ False（验证失败）
    """
    headers = get_github_headers(github_token)
    repo = config["target_repo"]
    branch = config["target_branch"]
    doc_path = config["feature_doc_path"]
    table_header = config["table_header"]
    required_sections = config["required_sections"]
    min_feat_count = config["min_feature_count"]
    expected_feats = config["expected_features"]
    expected_authors = config["expected_authors"]
    expected_msgs = config["expected_messages"]
    expected_dates = config["expected_dates"]

    print("=" * 60)
    print(f"📋 开始验证：{github_org}/{repo}@{branch}")
    print(f"📄 目标文档：{doc_path}")
    print("=" * 60)

    # 步骤1：获取特征文档内容
    print("\n1. 📥 获取特征文档...")
    doc_content = fetch_github_file(doc_path, headers, github_org, repo, branch)
    if not doc_content:
        return False
    print(f"✅ 成功获取文档（大小：{len(doc_content)} 字符）")

    # 步骤2：验证文档必填章节
    print(f"\n2. 📝 验证文档章节...")
    for section in required_sections:
        if section not in doc_content:
            print(f"❌ 缺失必填章节：「{section}」", file=sys.stderr)
            return False
        print(f"   ✅ 章节存在：{section}")
    print(f"✅ 所有 {len(required_sections)} 个必填章节均存在")

    # 步骤3：解析特征表格
    print(f"\n3. 🔍 解析特征表格...")
    features = parse_feature_table(doc_content, table_header)
    if len(features) == 0:
        print("❌ 未解析到任何特征", file=sys.stderr)
        print("   请检查：", file=sys.stderr)
        print("   - 表格标题是否正确配置", file=sys.stderr)
        print("   - 表格格式是否为标准Markdown表格", file=sys.stderr)
        print(f"   配置的表头：{table_header}", file=sys.stderr)
        return False
    print(f"✅ 解析到 {len(features)} 个特征")

    # 步骤4：验证特征数量
    print(f"\n4. 📊 验证特征数量...")
    if len(features) < min_feat_count:
        print(f"❌ 特征数量不足（预期≥{min_feat_count}，实际={len(features)}）", file=sys.stderr)
        return False
    print(f"✅ 特征数量满足要求（{len(features)} ≥ {min_feat_count}）")

    # 步骤5：验证预期特征与SHA
    print(f"\n5. 🔗 验证特征与SHA匹配...")
    feat_name_to_sha = {feat["name"]: feat["sha"] for feat in features}
    for expected_name, expected_sha in expected_feats.items():
        if expected_name not in feat_name_to_sha:
            print(f"❌ 预期特征「{expected_name}」未在表格中找到", file=sys.stderr)
            return False
        
        actual_sha = feat_name_to_sha[expected_name]
        if actual_sha != expected_sha:
            print(f"❌ 特征「{expected_name}」SHA不匹配：", file=sys.stderr)
            print(f"   预期：{expected_sha}", file=sys.stderr)
            print(f"   实际：{actual_sha}", file=sys.stderr)
            return False
        print(f"   ✅ 特征匹配：{expected_name} -> {expected_sha[:8]}...")
    print(f"✅ 所有 {len(expected_feats)} 个预期特征SHA均匹配")

    # 步骤6：验证提交详情
    print(f"\n6. 📅 验证提交详情...")
    verified_count = 0
    for feat in features:
        feat_sha = feat["sha"]
        if feat_sha not in expected_authors:
            continue
        
        print(f"   🔍 验证提交：{feat_sha[:8]}...")
        
        # 先验证SHA格式
        if not re.match(r'^[a-f0-9]{40}$', feat_sha):
            print(f"❌ 错误：无效的SHA格式 {feat_sha}（必须为40位小写十六进制）", file=sys.stderr)
            return False
            
        commit_detail = verify_commit(feat_sha, headers, github_org, repo)
        
        # 严格检查commit_detail是否为None或无效
        if not commit_detail or not isinstance(commit_detail, dict):
            print(f"❌ 错误：无法获取有效的提交详情 {feat_sha[:8]}", file=sys.stderr)
            return False

        # 验证作者（安全访问嵌套字典）
        expected_author = expected_authors.get(feat_sha)
        
        # 处理author可能为null的情况
        if commit_detail.get('author') is None:
            if expected_author is not None:
                print(f"❌ 错误：提交 {feat_sha[:8]} 预期作者为 {expected_author}，但实际没有关联用户", file=sys.stderr)
                return False
            print(f"   ⚠️ 警告：提交 {feat_sha[:8]} 没有关联GitHub用户（配置允许此情况）")
        else:
            actual_author = commit_detail.get("author", {}).get("login")
            if not actual_author or actual_author != expected_author:
                print(f"❌ 提交 {feat_sha[:8]} 作者不匹配：", file=sys.stderr)
                print(f"   预期：{expected_author}", file=sys.stderr)
                print(f"   实际：{actual_author or '空值'}", file=sys.stderr)
                return False

        # 验证提交信息
        expected_msg = expected_msgs.get(feat_sha, "")
        actual_commit_msg = commit_detail.get("commit", {}).get("message", "").split("\n")[0]
        if actual_commit_msg != expected_msg:
            print(f"❌ 提交 {feat_sha[:8]} GitHub信息不匹配：", file=sys.stderr)
            print(f"   预期：{expected_msg}", file=sys.stderr)
            print(f"   实际：{actual_commit_msg}", file=sys.stderr)
            return False

        # 验证日期
        expected_date = expected_dates.get(feat_sha)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", feat["date"]):
            print(f"❌ 特征「{feat['name']}」日期格式错误（应为YYYY-MM-DD）：{feat['date']}", file=sys.stderr)
            return False
        
        if feat["date"] != expected_date:
            print(f"❌ 提交 {feat_sha[:8]} 日期不匹配：", file=sys.stderr)
            print(f"   预期：{expected_date}", file=sys.stderr)
            print(f"   实际：{feat['date']}", file=sys.stderr)
            return False

        verified_count += 1
        print(f"   ✅ 提交验证通过：{feat_sha[:8]}...")

    print(f"✅ 所有 {verified_count} 个提交详情均验证通过")

    # 验证完成
    print("\n" + "=" * 60)
    print("🎉 所有验证步骤均通过！")
    print("=" * 60)
    return True

# ==========================
# 入口函数
# ==========================
def main():
    parser = argparse.ArgumentParser(description="GitHub功能提交跟踪验证脚本")
    parser.add_argument(
        "--config",
        type=str,
        default=DEFAULT_CONFIG_FILE,
        help=f"配置文件路径（默认：{DEFAULT_CONFIG_FILE}）"
    )
    parser.add_argument(
        "--env",
        type=str,
        default=DEFAULT_ENV_FILE,
        help=f"环境文件路径（默认：{DEFAULT_ENV_FILE}）"
    )
    args = parser.parse_args()

    try:
        # 加载环境变量
        print(f"📌 加载环境变量：{args.env}")
        github_token, github_org = load_environment(args.env)

        # 加载项目配置
        print(f"📌 加载项目配置：{args.config}")
        project_config = load_project_config(args.config)

        # 执行验证逻辑
        print("\n" + "-" * 50)
        verification_result = run_verification(project_config, github_token, github_org)

        sys.exit(0 if verification_result else 1)
    
    except Exception as e:
        print(f"🔥 未处理的顶层异常: {type(e).__name__} - {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()