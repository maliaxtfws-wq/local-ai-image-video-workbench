[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择本次任务生成图片的保存文件夹'
$dialog.ShowNewFolderButton = $true
$dialog.SelectedPath = [Environment]::GetFolderPath('MyDocuments')
$owner = New-Object System.Windows.Forms.Form
$owner.Text = '选择图片保存文件夹'
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Width = 1
$owner.Height = 1
$owner.Opacity = 0
$owner.Show()
try {
  if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    ConvertTo-Json -Compress -InputObject $dialog.SelectedPath
  } else { '""' }
} finally { $dialog.Dispose(); $owner.Close(); $owner.Dispose() }
