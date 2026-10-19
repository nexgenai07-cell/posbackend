@echo off
rem Sends pending ZATCA invoices and issues any missed ones, once.
rem Windows Task Scheduler runs this every 5 minutes (see the milestone 6 report).
rem Output (document numbers and statuses only, never a secret) is appended to
rem zatca_retry_queue.log next to manage.py; *.log is git-ignored.
cd /d "%~dp0.."
"venv\Scripts\python.exe" manage.py zatca_retry_queue >> "zatca_retry_queue.log" 2>&1
