$secure = Read-Host "Enter DeepSeek API key" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)

try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringUni($bstr)

    if ([string]::IsNullOrWhiteSpace($plain)) {
        Write-Error "DeepSeek API key is empty"
        exit 1
    }

    [Environment]::SetEnvironmentVariable("KB_LLM_API_KEY", $plain, "User")
    $env:KB_LLM_API_KEY = $plain
    Write-Output "KB_LLM_API_KEY set for the current Windows user"
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
