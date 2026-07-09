module.exports = {
  apps: [
    {
      name: "PAI-Insights-CRM",
      cwd: "/home/webadmin/ms-insights-portal-crm",
      script: "main.py",
      interpreter: "/home/webadmin/ms-insights-portal-crm/.venv/bin/python",
      instances: 1,
      autorestart: true,
      max_restarts: 10,
      env: {
        ONEPLATFORM_SERVICE__PORT: "8014",
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
