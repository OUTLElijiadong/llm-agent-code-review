rule Prism_EICAR_Test_File
{
    meta:
        description = "EICAR antivirus test signature"
        version = "2026-07-10"
        severity = "test"
    strings:
        $eicar = "EICAR-STANDARD-ANTIVIRUS-TEST-FILE" ascii
    condition:
        $eicar
}

rule Prism_High_Confidence_PHP_Webshell
{
    meta:
        description = "High confidence PHP eval plus base64 decode webshell pattern"
        version = "2026-07-10"
        severity = "high"
    strings:
        $php = "<?php" ascii nocase
        $eval = /eval\s*\(/ ascii nocase
        $decode = /base64_decode\s*\(/ ascii nocase
    condition:
        $php and $eval and $decode
}
