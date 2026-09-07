# PHP 生态漏洞参考库

核验日期：2026-09-07。共 41 条参考记录，来源为 CVE 官方记录和组件维护者公告。

**判定**：必须核对目标的精确组件版本、运行环境、配置及可达代码路径。版本命中只能形成候选，不能直接确认可利用。HTTP 非 404、500 或版本号相似均不足以确认漏洞。此参考集不是全量 NVD 数据库，不执行主动探测。

原始结构化记录及逐条来源哈希：`app/constants/data/verified_advisories.json`。

## 漏洞参考：CVE-2026-48019
- **来源标题**：CRLF injection in Laravel's default email rule enables SMTP smuggling and spoofed-mail relay
- **组件与受影响范围**：laravel / framework：>= 13.0.0, < 13.10.0；< 12.60.0
- **判定依据（官方原文）**：Laravel is a web application framework. Prior to versions 12.60.0 and 13.10.0, a CRLF injection vulnerability in Laravel's email validation, in combination with how Symfony Mailer and Symfony Mime handle certain character sequences, may allow an unauthenticated attacker to interfere with outbound email processing in applications that send mail to user-supplied addresses. This issue has been patched in versions 12.60.0 and 13.10.0.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-48019
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-09-04T22:11:40.811Z。

## 漏洞参考：CVE-2026-7260
- **来源标题**：Stack overflow in phar with circular symlinks
- **组件与受影响范围**：PHP Group / PHP / ext-phar：8.2.* 至 < 8.2.33；8.3.* 至 < 8.3.33；8.4.* 至 < 8.4.24；8.5.* 至 < 8.5.9
- **判定依据（官方原文）**：Circular symbolic links in phar archives could lead to unbounded recursion, exhausting the C stack and crashing the PHP process, in PHP versions from 8.2.* before 8.2.33, from 8.3.* before 8.3.33, from 8.4.* before 8.4.24, and from 8.5.* before 8.5.9.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-7260
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-30T12:15:22.862Z。

## 漏洞参考：CVE-2026-17544
- **来源标题**：Out-of-bounds write in bccomp() via crafted operand and scale
- **组件与受影响范围**：PHP Group / PHP / ext-bcmath：8.4.* 至 < 8.4.24；8.5.* 至 < 8.5.9
- **判定依据（官方原文）**：Attacker-provided inputs to bccomp() could lead to an out-of-bounds write with stack and heap corruption in PHP versions from 8.4.* before 8.4.24 and from 8.5.* before 8.5.9.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-17544
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-31T03:55:47.261Z。

## 漏洞参考：CVE-2026-17543
- **来源标题**：SQL injection in ext-pgsql via E'...' backslash breakout
- **组件与受影响范围**：PHP Group / PHP / ext-pgsql：8.2.* 至 < 8.2.33；8.3.* 至 < 8.3.33；8.4.* 至 < 8.4.24；8.5.* 至 < 8.5.9
- **判定依据（官方原文）**：Improper escaping of backslashes in attacker-provided parameters would allow for trivial SQL injection in PHP versions from 8.2.* before 8.2.33, from 8.3.* before 8.3.33, from 8.4.* before 8.4.24, and from 8.5.* before 8.5.9.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-17543
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-31T03:55:46.475Z。

## 漏洞参考：CVE-2026-9672
- **来源标题**：PHP 更新所捆绑的 libgd
- **组件与受影响范围**：libgd / PHP GD：以部署环境实际 libgd 版本及上游安全补丁为准；PHP 8.2.33、8.3.33、8.4.24、8.5.9 变更日志列出该修复。
- **判定依据（官方原文）**：PHP 官方变更日志明确列出通过升级 libgd 修复此 CVE。该发布说明没有提供完整 libgd 受影响版本范围，不能从 PHP 版本或扩展存在直接判断可利用。
- **官方来源**：https://www.php.net/ChangeLog-8.php
- **来源类型**：组件维护者官方公告（中文摘要）；记录更新时间：来源未提供。

## 漏洞参考：CVE-2026-14355
- **来源标题**：ext/openssl: Memory corruption in openssl_encrypt with AES-WRAP-PAD
- **组件与受影响范围**：php / php / openssl：8.2.0 至 < 8.2.32；8.3.0 至 < 8.3.32；8.4.0 至 < 8.4.23；8.5.0 至 < 8.5.8
- **判定依据（官方原文）**：In PHP versions 8.2.* before 8.2.32, 8.3.* before 8.3.32, 8.4.* before 8.4.23, 8.5.* before 8.5.8, the AES-WRAP-PAD algorithm implementation in OpenSSL extension contains a buffer allocation flaw. The output buffer for the AES key-wrap-with-padding operation is sized from the plaintext length without accounting for RFC 5649 expansion. This may cause OpenSSL to write beyond allocated memory, corrupting heap metadata and triggering application abort.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-14355
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-06T13:57:58.387Z。

## 漏洞参考：CVE-2026-12184
- **来源标题**：Failure to setup TLS with a remote server can result in a remote DoS
- **组件与受影响范围**：PHP：维护者公告按分支列 <8.3.32、<8.4.21、<8.5.6；修复分别为 8.3.32、8.4.21、8.5.6。
- **判定依据（官方原文）**：PHP HTTP 流在 TLS 加密初始化失败后关闭流，但随后清理仍访问该流，可能触发进程崩溃。需核对 TLS 失败路径与 PHP 运行方式。
- **官方来源**：https://github.com/php/php-src/security/advisories/GHSA-mhmq-mmqj-2v39
- **来源类型**：组件维护者官方公告（中文摘要）；记录更新时间：来源未提供。

## 漏洞参考：CVE-2026-48041
- **来源标题**：Temporary Signed URL Path Confusion
- **组件与受影响范围**：laravel/framework：官方公告分别列出 <13.12.0、<12.61.1；修复为 13.12.0、12.61.1。须按主版本分支核对。
- **判定依据（官方原文）**：Laravel 本地文件系统临时签名 URL 的解析歧义可能使请求访问非预期资源或绕过过期校验；上传 URL 也可能写入非预期目标。需验证是否使用相关签名 URL 流程。
- **官方来源**：https://github.com/laravel/framework/security/advisories/GHSA-crmm-hgp2-wgrp
- **来源类型**：组件维护者官方公告（中文摘要）；记录更新时间：来源未提供。

## 漏洞参考：CVE-2026-7263
- **来源标题**：DoS attack via DOMNode::C14N()
- **组件与受影响范围**：PHP Group / PHP / dom：8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.4.* before 8.4.21 and 8.5.* before 8.5.6, DOMNode::C14N() method may process the XML data incorrectly, causing a circular linked list in the data structure representing the XML document. This may cause subsequent processing of the XML document to enter infinite loop, causing denial of service in the processing application.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-7263
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-15T00:40:16.236Z。

## 漏洞参考：CVE-2026-6104
- **来源标题**：Global buffer over-read in mb_convert_encoding() with attacker-supplied encoding
- **组件与受影响范围**：PHP Group / PHP / mbstring：8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.4.* before 8.4.21 and 8.5.* before 8.5.6, when an encoding name containing an embedded NUL byte is passed to mb_convert_encoding() or related mbstring functions, the code incorrectly assumes that when strncasecmp() returns 0 it means the strings have the same length. This can lead to out-of-bounds read of global memory, potentially causing a crash or information disclosure or crash. Affected functions include mb_convert_encoding(), mb_detect_encoding(), mb_convert_variables(), and mb_detect_order(), as well as the mbstring.detect_order and mbstring.http_output INI settings.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-6104
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-15T00:41:42.455Z。

## 漏洞参考：CVE-2026-7258
- **来源标题**：Out-of-bounds read in urldecode() on NetBSD
- **组件与受影响范围**：PHP Group / PHP：8.2.* 至 < 8.2.31；8.3.* 至 < 8.3.31；8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.2.* before 8.2.31, 8.3.* before 8.3.31, 8.4.* before 8.4.21, and 8.5.* before 8.5.6, some functions, including urldecode(), pass signed char to ctype functions (like isxdigit()). On the systems with default signed char and optimized table-lookup ctype functions - such as NetBSD - this can lead to accessing array with negative offset, which can trigger a denial of service.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-7258
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-05-11T13:06:10.908Z。

## 漏洞参考：CVE-2026-6722
- **来源标题**：Use-After-Free in SOAP using Apache map
- **组件与受影响范围**：PHP Group / PHP / soap：8.2.* 至 < 8.2.31；8.3.* 至 < 8.3.31；8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.2.* before 8.2.31, 8.3.* before 8.3.31, 8.4.* before 8.4.21, and 8.5.* before 8.5.6, the SOAP extension's object deduplication mechanism stores pointers to PHP objects in a global map without incrementing their reference counts. When an apache:Map node contains duplicate keys, processing the second entry overwrites the first in the temporary result map, freeing the original PHP object while its stale pointer remains in the map. A subsequent href reference to the freed node can copy the dangling pointer into the result. As PHP string allocations can reclaim the freed memory region, an attacker with control over the SOAP request body can exploit this use-after-free to achieve remote code execution.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-6722
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-23T12:08:11.781Z。

## 漏洞参考：CVE-2026-7259
- **来源标题**：Null pointer dereference in php_mb_check_encoding() via mb_ereg_search_init()
- **组件与受影响范围**：PHP Group / PHP / mbstring：8.2.* 至 < 8.2.31；8.3.* 至 < 8.3.31；8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.2.* before 8.2.31, 8.3.* before 8.3.31, 8.4.* before 8.4.21, and 8.5.* before 8.5.6, a mismatch between encoding lists in Oniguruma and mbfl leads to  a NULL pointer dereference, resulting in a segmentation fault and denial of service. The vulnerability is exploitable when user-controlled input can influence the encoding passed to mb_regex_encoding().
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-7259
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-05-11T13:13:50.416Z。

## 漏洞参考：CVE-2026-7261
- **来源标题**：SoapServer session-persisted object use-after-free via SOAP header fault
- **组件与受影响范围**：PHP Group / PHP / soap：8.2.* 至 < 8.2.31；8.3.* 至 < 8.3.31；8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.2.* before 8.2.31, 8.3.* before 8.3.31, 8.4.* before 8.4.21, and 8.5.* before 8.5.6, when SoapServer is configured with SOAP_PERSISTENCE_SESSION, the handler object is persisted across requests via session storage. However, in the case SOAP requests results in an error, the persistance is handled incorrectly, resulting in freeing the object while keeping a pointer to it, which may lead to use-after-free. This may lead to memory corruption, information disclosure, or process crashes, with confidentiality, integrity, and availability impact on the vulnerable system.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-7261
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-05-11T13:14:26.451Z。

## 漏洞参考：CVE-2026-7262
- **来源标题**：NULL pointer dereference in SOAP apache:Map decoder with missing <value>
- **组件与受影响范围**：PHP Group / PHP / soap：8.2.* 至 < 8.2.31；8.3.* 至 < 8.3.31；8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.2.* before 8.2.31, 8.3.* before 8.3.31, 8.4.* before 8.4.21, and 8.5.* before 8.5.6, when a SOAP server has a typemap configured, the decoding process contains a mistake which checks the wrong variable in case of missing value element.  This leads to dereferences a NULL pointer, causing a segmentation fault. This allows a remote unauthenticated attacker to crash the PHP SOAP server process, resulting in denial of service.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-7262
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-15T00:40:18.277Z。

## 漏洞参考：CVE-2026-7568
- **来源标题**：Signed integer overflow in metaphone()
- **组件与受影响范围**：PHP Group / PHP：8.2.* 至 < 8.2.31；8.3.* 至 < 8.3.31；8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.2.* before 8.2.31, 8.3.* before 8.3.31, 8.4.* before 8.4.21, and 8.5.* before 8.5.6, the metaphone() function in ext/standard/metaphone.c uses a signed int variable to track the current position within the input string. If a string longer than 2,147,483,647 bytes is passed, a signed integer overflow occurs, resulting in undefined behavior. This can lead to an out-of-bounds read, causing a segmentation fault or access to unrelated memory, and may affect the availability of the PHP process.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-7568
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-07-15T00:39:53.392Z。

## 漏洞参考：CVE-2026-6735
- **来源标题**：XSS within PHP-FPM status endpoint
- **组件与受影响范围**：PHP Group / PHP：8.2.* 至 < 8.2.31；8.3.* 至 < 8.3.31；8.4.* 至 < 8.4.21；8.5.* 至 < 8.5.6
- **判定依据（官方原文）**：In PHP versions 8.2.* before 8.2.31, 8.3.* before 8.3.31, 8.4.* before 8.4.21, 8.5.* before 8.5.6, due to improper sanitation of user data, it allows an attacker to compose an URL, which will cause the target to execute arbitrary JavaScript code (XSS) on the target's machine when the target is viewing the PHP-FPM status page.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-6735
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-05-11T13:25:54.957Z。

## 漏洞参考：CVE-2026-44928
- **来源标题**：In uriparser before 1.0.2, the function family EqualsUri can misclassify two unequal URIs as equal.
- **组件与受影响范围**：uriparser / uriparser：0 至 < 1.0.2
- **判定依据（官方原文）**：In uriparser before 1.0.2, the function family EqualsUri can misclassify two unequal URIs as equal.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-44928
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-05-10T05:45:32.602Z。

## 漏洞参考：CVE-2026-44927
- **来源标题**：In uriparser before 1.0.2, there is pointer difference truncation to int in various places.
- **组件与受影响范围**：uriparser / uriparser：0 至 < 1.0.2
- **判定依据（官方原文）**：In uriparser before 1.0.2, there is pointer difference truncation to int in various places.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-44927
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-05-10T05:46:18.606Z。

## 漏洞参考：CVE-2026-42371
- **来源标题**：uriparser before 1.0.1 has numeric truncation in text range comparison, if an application accepts URIs with a length in gigabytes.
- **组件与受影响范围**：uriparser / uriparser：0 至 < 1.0.1
- **判定依据（官方原文）**：uriparser before 1.0.1 has numeric truncation in text range comparison, if an application accepts URIs with a length in gigabytes.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2026-42371
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-04-27T14:41:22.410Z。

## 漏洞参考：CVE-2024-13919
- **来源标题**：Laravel Reflected XSS via Route Parameter in Debug-Mode Error Page
- **组件与受影响范围**：Laravel Holdings Inc. / Laravel Framework：11.9.0 至 ≤ 11.35.1
- **判定依据（官方原文）**：The Laravel framework versions between 11.9.0 and 11.35.1 are susceptible to reflected cross-site scripting due to an improper encoding of route parameters in the debug-mode error page.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2024-13919
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-03-10T17:02:42.335Z。

## 漏洞参考：CVE-2024-13918
- **来源标题**：Laravel Reflected XSS via Request Parameter in Debug-Mode Error Page
- **组件与受影响范围**：Laravel Holdings Inc. / Laravel Framework：11.9.0 至 ≤ 11.35.1
- **判定依据（官方原文）**：The Laravel framework versions between 11.9.0 and 11.35.1 are susceptible to reflected cross-site scripting due to an improper encoding of request parameters in the debug-mode error page.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2024-13918
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-03-10T17:02:40.794Z。

## 漏洞参考：CVE-2025-27515
- **来源标题**：Laravel has a File Validation Bypass
- **组件与受影响范围**：laravel / framework：>= 12.0.0, < 12.1.1；< 11.44.1
- **判定依据（官方原文）**：Laravel is a web application framework. When using wildcard validation to validate a given file or image field (`files.*`), a user-crafted malicious request could potentially bypass the validation rules. This vulnerability is fixed in 11.44.1 and 12.1.1.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2025-27515
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-03-05T18:59:49.627Z。

## 漏洞参考：CVE-2024-52301
- **来源标题**：Laravel allows environment manipulation via query string
- **组件与受影响范围**：laravel / framework：< 6.20.45；>= 7.0.0, < 7.30.7；>= 8.0.0, < 8.83.28；>= 9.0.0, < 9.52.17；>= 10.0.0, < 10.48.23；>= 11.0.0, < 11.31.0
- **判定依据（官方原文）**：Laravel is a web application framework. When the register_argc_argv php directive is set to on , and users call any URL with a special crafted query string, they are able to change the environment used by the framework when handling the request. The vulnerability fixed in 6.20.45, 7.30.7, 8.83.28, 9.52.17, 10.48.23, and 11.31.0. The framework now ignores argv values for environment detection on non-cli SAPIs.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2024-52301
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2024-12-21T17:02:39.839Z。

## 漏洞参考：CVE-2024-4577
- **来源标题**：Argument Injection in PHP-CGI
- **组件与受影响范围**：PHP Group / PHP：8.1.* 至 < 8.1.29；8.2.* 至 < 8.2.20；8.3.* 至 < 8.3.8
- **判定依据（官方原文）**：In PHP versions 8.1.* before 8.1.29, 8.2.* before 8.2.20, 8.3.* before 8.3.8, when using Apache and PHP-CGI on Windows, if the system is set up to use certain code pages, Windows may use "Best-Fit" behavior to replace characters in command line given to Win32 API functions. PHP CGI module may misinterpret those characters as PHP options, which may allow a malicious user to pass options to PHP binary being run, and thus reveal the source code of scripts, run arbitrary PHP code on the server, etc.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2024-4577
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-10-21T23:05:16.089Z。

## 漏洞参考：CVE-2023-2745
- **来源标题**：WordPress Core < 6.2.1 - Directory Traversal
- **组件与受影响范围**：WordPress Foundation / WordPress：0 至 < 4.1.38；4.2 至 < 4.2.35；4.3 至 < 4.3.31；4.4 至 < 4.4.30；4.5 至 < 4.5.29；4.6 至 < 4.6.26；4.7 至 < 4.7.26；4.8 至 < 4.8.22；4.9 至 < 4.9.23；5.0 至 < 5.0.19；5.1 至 < 5.1.16；5.2 至 < 5.2.18；5.3 至 < 5.3.15；5.4 至 < 5.4.13；5.5 至 < 5.5.12；5.6 至 < 5.6.11；5.7 至 < 5.7.9；5.8 至 < 5.8.7；5.9 至 < 5.9.6；6.0 至 < 6.0.4；6.1 至 < 6.1.2；6.2 至 < 6.2.1
- **判定依据（官方原文）**：WordPress Core is vulnerable to Directory Traversal in versions up to, and including, 6.2, via the ‘wp_lang’ parameter. This allows unauthenticated attackers to access and load arbitrary translation files. In cases where an attacker is able to upload a crafted translation file onto the site, such as via an upload form, this could be also used to perform a Cross-Site Scripting attack.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2023-2745
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2026-04-08T17:31:40.202Z。

## 漏洞参考：CVE-2022-31043
- **来源标题**：Fix failure to strip Authorization header on HTTP downgrade in Guzzle
- **组件与受影响范围**：guzzle / guzzle：< 6.5.7；>=7.0.0, < 7.4.4
- **判定依据（官方原文）**：Guzzle is an open source PHP HTTP client. In affected versions `Authorization` headers on requests are sensitive information. On making a request using the `https` scheme to a server which responds with a redirect to a URI with the `http` scheme, we should not forward the `Authorization` header on. This is much the same as to how we don't forward on the header if the host changes. Prior to this fix, `https` to `http` downgrades did not result in the `Authorization` header being removed, only changes to the host. Affected Guzzle 7 users should upgrade to Guzzle 7.4.4 as soon as possible. Affected users using any earlier series of Guzzle should upgrade to Guzzle 6.5.7 or 7.4.4. Users unable to upgrade may consider an alternative approach which would be to use their own redirect middleware. Alternately users may simply disable redirects all together if redirects are not expected or required.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2022-31043
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-04-23T18:18:11.546Z。

## 漏洞参考：CVE-2022-31042
- **来源标题**：Failure to strip the Cookie header on change in host or HTTP downgrade in Guzzle
- **组件与受影响范围**：guzzle / guzzle：< 6.5.7；>=7.0.0, < 7.4.4
- **判定依据（官方原文）**：Guzzle is an open source PHP HTTP client. In affected versions the `Cookie` headers on requests are sensitive information. On making a request using the `https` scheme to a server which responds with a redirect to a URI with the `http` scheme, or on making a request to a server which responds with a redirect to a a URI to a different host, we should not forward the `Cookie` header on. Prior to this fix, only cookies that were managed by our cookie middleware would be safely removed, and any `Cookie` header manually added to the initial request would not be stripped. We now always strip it, and allow the cookie middleware to re-add any cookies that it deems should be there. Affected Guzzle 7 users should upgrade to Guzzle 7.4.4 as soon as possible. Affected users using any earlier series of Guzzle should upgrade to Guzzle 6.5.7 or 7.4.4. Users unable to upgrade may consider an alternative approach to use your own redirect middleware, rather than ours. If you do not require or expect redirects to be followed, one should simply disable redirects all together.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2022-31042
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-04-23T18:18:20.749Z。

## 漏洞参考：CVE-2022-29248
- **来源标题**：Cross-domain cookie leakage in Guzzle
- **组件与受影响范围**：guzzle / guzzle：< 6.5.6；>= 7.0.0, < 7.4.3
- **判定依据（官方原文）**：Guzzle is a PHP HTTP client. Guzzle prior to versions 6.5.6 and 7.4.3 contains a vulnerability with the cookie middleware. The vulnerability is that it is not checked if the cookie domain equals the domain of the server which sets the cookie via the Set-Cookie header, allowing a malicious server to set cookies for unrelated domains. The cookie middleware is disabled by default, so most library consumers will not be affected by this issue. Only those who manually add the cookie middleware to the handler stack or construct the client with ['cookies' => true] are affected. Moreover, those who do not use the same Guzzle client to call multiple domains and have disabled redirect forwarding are not affected by this vulnerability. Guzzle versions 6.5.6 and 7.4.3 contain a patch for this issue. As a workaround, turn off the cookie middleware.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2022-29248
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-04-23T18:21:56.089Z。

## 漏洞参考：CVE-2022-21661
- **来源标题**：SQL injection in WordPress
- **组件与受影响范围**：WordPress / wordpress-develop：< 5.8.3
- **判定依据（官方原文）**：WordPress is a free and open-source content management system written in PHP and paired with a MariaDB database. Due to improper sanitization in WP_Query, there can be cases where SQL injection is possible through plugins or themes that use it in a certain way. This has been patched in WordPress version 5.8.3. Older affected versions are also fixed via security release, that go back till 3.7.37. We strongly recommend that you keep auto-updates enabled. There are no known workarounds for this vulnerability.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2022-21661
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2024-09-09T14:13:15.875Z。

## 漏洞参考：CVE-2021-21705
- **来源标题**：Incorrect URL validation in FILTER_VALIDATE_URL
- **组件与受影响范围**：PHP Group / PHP：7.3.x 至 < 7.3.29；7.4.x 至 < 7.4.21；8.0.X 至 < 8.0.8
- **判定依据（官方原文）**：In PHP versions 7.3.x below 7.3.29, 7.4.x below 7.4.21 and 8.0.x below 8.0.8, when using URL validation functionality via filter_var() function with FILTER_VALIDATE_URL parameter, an URL with invalid password field can be accepted as valid. This can lead to the code incorrectly parsing the URL and potentially leading to other security implications - like contacting a wrong server or making a wrong access decision.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2021-21705
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2024-09-17T04:09:29.556Z。

## 漏洞参考：CVE-2021-21702
- **来源标题**：Null Dereference in SoapClient
- **组件与受影响范围**：PHP Group / PHP：7.3.x 至 < 7.3.27；7.4.x 至 < 7.4.15；8.0.X 至 < 8.0.2
- **判定依据（官方原文）**：In PHP versions 7.3.x below 7.3.27, 7.4.x below 7.4.15 and 8.0.x below 8.0.2, when using SOAP extension to connect to a SOAP server, a malicious SOAP server could return malformed XML data as a response that would cause PHP to access a null pointer and thus cause a crash.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2021-21702
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2024-09-16T17:34:26.201Z。

## 漏洞参考：CVE-2021-21263
- **来源标题**：Query Binding Exploitation in Laravel
- **组件与受影响范围**：laravel / framework：>= 6.0.0, < 6.20.11；>= 7.0.0, < 7.30.2；>= 8.0.0, < 8.22.1
- **判定依据（官方原文）**：Laravel is a web application framework. Versions of Laravel before 6.20.11, 7.30.2 and 8.22.1 contain a query binding exploitation. This same exploit applies to the illuminate/database package which is used by Laravel. If a request is crafted where a field that is normally a non-array value is an array, and that input is not validated or cast to its expected type before being passed to the query builder, an unexpected number of query bindings can be added to the query. In some situations, this will simply lead to no results being returned by the query builder; however, it is possible certain queries could be affected in a way that causes the query to return unexpected results.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2021-21263
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2024-08-03T18:09:14.909Z。

## 漏洞参考：CVE-2021-3129
- **来源标题**：Ignition before 2.5.2, as used in Laravel and other products, allows unauthenticated remote attackers to execute arbitrary code because of insecure usage of file_get_contents() and file_put_contents()
- **组件与受影响范围**：：以官方描述中的版本和前提为准
- **判定依据（官方原文）**：Ignition before 2.5.2, as used in Laravel and other products, allows unauthenticated remote attackers to execute arbitrary code because of insecure usage of file_get_contents() and file_put_contents(). This is exploitable on sites using debug mode with Laravel before 8.4.2.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2021-3129
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-10-21T23:35:30.227Z。

## 漏洞参考：CVE-2019-11043
- **来源标题**：Underflow in PHP-FPM can lead to RCE
- **组件与受影响范围**：PHP / PHP：7.1.x 至 < 7.1.33；7.2.x 至 < 7.2.24；7.3.x 至 < 7.3.11
- **判定依据（官方原文）**：In PHP versions 7.1.x below 7.1.33, 7.2.x below 7.2.24 and 7.3.x below 7.3.11 in certain configurations of FPM setup it is possible to cause FPM module to write past allocated buffers into the space reserved for FCGI protocol data, thus opening the possibility of remote code execution.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2019-11043
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-10-21T23:45:28.408Z。

## 漏洞参考：CVE-2019-8942
- **来源标题**：WordPress before 4.9.9 and 5.x before 5.0.1 allows remote code execution because an _wp_attached_file Post Meta entry can be changed to an arbitrary string, such as one ending with a .jpg?file.php substring
- **组件与受影响范围**：：以官方描述中的版本和前提为准
- **判定依据（官方原文）**：WordPress before 4.9.9 and 5.x before 5.0.1 allows remote code execution because an _wp_attached_file Post Meta entry can be changed to an arbitrary string, such as one ending with a .jpg?file.php substring. An attacker with author privileges can execute arbitrary code by uploading a crafted image containing PHP code in the Exif metadata. Exploitation can leverage CVE-2019-8943.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2019-8942
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2024-08-04T21:31:37.541Z。

## 漏洞参考：CVE-2018-15133
- **来源标题**：In Laravel Framework through 5.5.40 and 5.6.x through 5.6.29, remote code execution might occur as a result of an unserialize call on a potentially untrusted X-XSRF-TOKEN value
- **组件与受影响范围**：：以官方描述中的版本和前提为准
- **判定依据（官方原文）**：In Laravel Framework through 5.5.40 and 5.6.x through 5.6.29, remote code execution might occur as a result of an unserialize call on a potentially untrusted X-XSRF-TOKEN value. This involves the decrypt method in Illuminate/Encryption/Encrypter.php and PendingBroadcast in gadgetchains/Laravel/RCE/3/chain.php in phpggc. The attacker must know the application key, which normally would never occur, but could happen if the attacker previously had privileged access or successfully accomplished a previous attack.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2018-15133
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-10-21T23:45:49.105Z。

## 漏洞参考：CVE-2017-9841
- **来源标题**：Util/PHP/eval-stdin.php in PHPUnit before 4.8.28 and 5.x before 5.6.3 allows remote attackers to execute arbitrary PHP code via HTTP POST data beginning with a "<?php " substring, as demonstrated by an attack on a site with an exposed /vendor folder, i.e., external access to the /vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php URI.
- **组件与受影响范围**：：以官方描述中的版本和前提为准
- **判定依据（官方原文）**：Util/PHP/eval-stdin.php in PHPUnit before 4.8.28 and 5.x before 5.6.3 allows remote attackers to execute arbitrary PHP code via HTTP POST data beginning with a "<?php " substring, as demonstrated by an attack on a site with an exposed /vendor folder, i.e., external access to the /vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php URI.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2017-9841
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-10-21T23:55:39.301Z。

## 漏洞参考：CVE-2017-1001000
- **来源标题**：The register_routes function in wp-includes/rest-api/endpoints/class-wp-rest-posts-controller.php in the REST API in WordPress 4.7.x before 4.7.2 does not require an integer identifier, which allows remote attackers to modify arbitrary pages via a request for wp-json/wp/v2/posts followed by a numeric value and a non-numeric value, as demonstrated by the wp-json/wp/v2/posts/123?id=123helloworld URI.
- **组件与受影响范围**：：以官方描述中的版本和前提为准
- **判定依据（官方原文）**：The register_routes function in wp-includes/rest-api/endpoints/class-wp-rest-posts-controller.php in the REST API in WordPress 4.7.x before 4.7.2 does not require an integer identifier, which allows remote attackers to modify arbitrary pages via a request for wp-json/wp/v2/posts followed by a numeric value and a non-numeric value, as demonstrated by the wp-json/wp/v2/posts/123?id=123helloworld URI.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2017-1001000
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2024-08-05T22:00:41.671Z。

## 漏洞参考：CVE-2016-10045
- **来源标题**：The isMail transport in PHPMailer before 5.2.20 might allow remote attackers to pass extra parameters to the mail command and consequently execute arbitrary code by leveraging improper interaction between the escapeshellarg function and internal escaping performed in the mail function in PHP
- **组件与受影响范围**：：以官方描述中的版本和前提为准
- **判定依据（官方原文）**：The isMail transport in PHPMailer before 5.2.20 might allow remote attackers to pass extra parameters to the mail command and consequently execute arbitrary code by leveraging improper interaction between the escapeshellarg function and internal escaping performed in the mail function in PHP. NOTE: this vulnerability exists because of an incorrect fix for CVE-2016-10033.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2016-10045
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2024-08-06T03:07:32.139Z。

## 漏洞参考：CVE-2016-10033
- **来源标题**：The mailSend function in the isMail transport in PHPMailer before 5.2.18 might allow remote attackers to pass extra parameters to the mail command and consequently execute arbitrary code via a \" (backslash double quote) in a crafted Sender property.
- **组件与受影响范围**：：以官方描述中的版本和前提为准
- **判定依据（官方原文）**：The mailSend function in the isMail transport in PHPMailer before 5.2.18 might allow remote attackers to pass extra parameters to the mail command and consequently execute arbitrary code via a \" (backslash double quote) in a crafted Sender property.
- **官方来源**：https://cveawg.mitre.org/api/cve/CVE-2016-10033
- **来源类型**：CVE 官方记录（CNA）；记录更新时间：2025-10-21T23:55:47.202Z。

## 历史资料纠正

旧速查将 CVE-2021-21381 归为 Symfony 调试远程代码执行，官方记录实际为 Flatpak 的沙箱逃逸，已从 PHP 活动参考集移除。
Guzzle 的 CVE-2022-29248/31042/31043 为重定向或跨域敏感头/凭据泄露，不能仅据此宣称所有版本有 SSRF。
未附官方编号或未验证版本范围的 ThinkPHP、Monolog、Twig 旧条目仅能作为一般代码模式线索，不能作为已确认组件 CVE。历史审查结果不因本库更新而改写。
