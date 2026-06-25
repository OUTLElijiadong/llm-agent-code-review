/**
 * XSS 漏洞样本(CWE-79)
 *
 * 演示 DOM XSS 通过 innerHTML 拼接用户输入的危险写法。
 * 静态规则未覆盖 JS XSS,此样本主要靠 LLM 深度审查识别。
 */

function renderComment(commentText) {
    // 漏洞:直接将用户输入拼接到 innerHTML,可注入 <script> 标签
    const container = document.getElementById('comment');
    container.innerHTML = '<div class="comment">' + commentText + '</div>';
}

function showSearchResult(keyword) {
    // 漏洞:document.write 拼接用户输入
    document.write('<h2>搜索结果: ' + keyword + '</h2>');
}

function handleRedirect(targetUrl) {
    // 漏洞:开放重定向 + 可能的 XSS
    location.href = targetUrl;
}
