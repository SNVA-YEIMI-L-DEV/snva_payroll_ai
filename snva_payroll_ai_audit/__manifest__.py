{
    "name": "Sinova Payroll AI Audit",
    "version": "19.0.1.0.0",
    "summary": "Smart Audit / Diagnostics for Payroll using Odoo 19 AI",
    "description": """
Sinova Payroll AI Audit
=======================
Non-intrusive Smart Audit for hr.payslip to eliminate support tickets
caused by configuration/parameterization mistakes.

Features:
- Button 'Validar Configuración (IA)' on hr.payslip form
- Payload serialization (employee, contract, worked days, line_ids, variables)
- System Prompt + JSON payload sent to Odoo 19 ai.service / LLM backend (LLMApiService)
- Strict JSON response parsing -> ai_audit_status / summary / json
- Native diagnostic banner (ok / warning / error) + chatter message
- Offline rule-based fallback when LLM unavailable

Rules audited:
1. Retención en la Fuente (RET_FTE): $0 but gross > UVT threshold → check retention_procedure
2. Provisions (Primas, Cesantías, Vacaciones, etc): $0 → check base variable linkage
3. Zero-value core rules: 0 due to unlinked accounts / missing parameters

Author: SINOVA SAS - https://sinova.co
    """,
    "category": "Human Resources",
    "author": "SINOVA SAS",
    "website": "https://sinova.co",
    "license": "LGPL-3",
    "depends": [
        "hr_payroll",
        "mail",
        "ai",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/ai_prompt_data.xml",
        "views/hr_payslip_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_backend": [],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
