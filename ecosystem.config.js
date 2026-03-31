module.exports = {
  apps: [{
    name: 'polymarket-bot',
    script: 'venv/bin/python',
    args: 'main.py',
    cwd: '/home/polybot/polymarket-agent',
    watch: false,
    max_restarts: 10,
    restart_delay: 5000,
    env: {
      DRY_RUN: 'True',
      PYTHONUNBUFFERED: '1'
    },
    error_file: 'logs/pm2-error.log',
    out_file: 'logs/pm2-out.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss'
  }]
}
