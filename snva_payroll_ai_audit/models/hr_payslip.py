# Part of SINOVA. See LICENSE file for full copyright and licensing details.
import json
import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System Prompt - also stored in data/ai_prompt_data.xml as ir.config_parameter
# ---------------------------------------------------------------------------
SINOVA_AUDIT_SYSTEM_PROMPT = """You are a Senior Payroll Configuration Auditor for Sinova products on Odoo 19.
Analyze the provided JSON payload of an `hr.payslip` record to find USER CONFIGURATION ERRORS that cause the payslip to be wrong, incomplete or miscalculated.

You must audit ALL cases surrounding the payslip, not only zero values. Distinguish clearly if the fix is user configuration (no dev needed) vs code/dev needed.

Rules to audit:
1. Retención en la Fuente (RET_FTE): If calculated total is $0 but gross income > minimum taxable base (UVT threshold), verify if Contract has 'Procedimiento 1 or 2' configured. If missing, report as warning (user config).
2. Provisions (Primas, Cesantías, Vacaciones, Intereses): If rule total is $0, check if linked base variable is present in the contract or payslip inputs. If missing -> user config.
3. Zero-value Core Rules: If a core rule (BASIC, GROSS, NET) is $0 but contract wage >0 or gross expected >0, likely unlinked accounts or missing parameters -> user config. If rule is expected to be $0 (e.g. auxilio not applicable) do NOT flag.
4. Duplicate Rule Codes: If payload.structure_rules shows two or more active hr.salary.rule with same code in same structure (duplicates list), report as error. The engine picks one arbitrarily and calculations become wrong. Fix: Nómina > Configuración > Reglas salariales > desactive o renombre duplicado (user config, no dev).
5. Rules Without Accounts: If any rule in structure_rules has no accounting lines for the payslip's accounting_type/company, flag as warning. Fix: en la regla pestaña Contabilidad vincule cuenta.
6. Missing Contract Data: If contract missing wage, contract_type, resource_calendar, schedule_pay, wage_type, or employee missing, report. Also if payslip dates outside contract dates.
7. Worked Days Inconsistency: If worked_days is empty but contract expects time, or number_of_days/hours inconsistent with structure_type/calendar, or duration inaccurate flag from payload -> warn to check Estructura Tipo y Calendario.
8. Empty Payslip Lines: If payslip_lines empty or only basic, structure may not be assigned correctly or rules filtered by python condition/domain. Flag to check Estructura Salarial y condiciones de reglas.
9. Inputs Missing: If rule expects an input (e.g. overtime, bonuses) and inputs list has no entry for that code, mention that total may be low due to missing input, not bug.

For each finding specify if it is user-configurable (no dev) or needs dev (e.g. python formula error). Prefer user config when possible.

OUTPUT FORMAT (STRICT JSON ONLY):
{
  "status": "ok | warning | error",
  "has_issues": true|false,
  "summary": "Brief 1-2 sentence overall diagnosis. Mention if fallback rules were used.",
  "findings": [
    {
      "severity": "warning | error",
      "rule_code": "Code of the rule involved or GENERAL",
      "problem": "Clear explanation of the missing setup or duplicate/wrong data",
      "solution": "Exact step in Odoo UI for the user to fix it, or 'Requiere desarrollo' if code fix needed"
    }
  ]
}
Do not add markdown, comments or extra text. Return ONLY valid JSON.
"""

# Fallback constants when annual_data / UVT not configured
DEFAULT_UVT_VALUE = 47065  # 2024 reference
DEFAULT_UVT_THRESHOLD_PESOS = int(DEFAULT_UVT_VALUE * 95)  # ~95 UVT as taxable base example
CORE_RULE_CODES = ["BASIC", "GROSS", "NET", "RET_FTE", "DEretencion", "RETENCION"]
PROVISION_CODES = [
    "CL_PROV_PRIMA", "CL_PROV_CES", "CL_PROV_VAC", "CL_PROV_INT_CES",
    "SP_PROV_PRIMA", "SP_PROV_CES", "SP_PROV_VAC", "SP_PROV_INT_CES",
    "PRIMA", "CESANTIAS", "INTERESES_CESANTIAS", "VACACIONES", "VAC", "PRIMA_L",
]
RETENTION_CODES = ["RET_FTE", "DEretencion", "RTF_PRIMA", "PRIMARTF", "RETENCION"]


class HrPayslip(models.Model):
    _inherit = "hr.payslip"

    ai_audit_status = fields.Selection(
        selection=[
            ("draft", "Not Audited"),
            ("ok", "No Issues"),
            ("warning", "Config Warning"),
            ("error", "Critical Error"),
        ],
        string="AI Audit Status",
        default="draft",
        tracking=True,
        copy=False,
        help="Result of the Smart Audit / Diagnostics",
    )
    ai_audit_summary = fields.Text(
        string="AI Audit Summary",
        copy=False,
        help="Main diagnostic summary returned by LLM",
    )
    ai_audit_json = fields.Text(
        string="AI Audit JSON",
        copy=False,
        help="Raw structured JSON output for debugging",
    )

    # -----------------------------------------------------------------------
    # Payload extraction
    # -----------------------------------------------------------------------
    def _get_sinova_audit_payload(self):
        """Serialize payslip data for LLM audit.

        Includes: employee, contract, worked_days, lines, inputs, plus full structure/rules audit data
        (duplicates, missing accounts, contract dates, structure) so LLM can diagnose all surrounding cases.
        """
        self.ensure_one()
        version = self.version_id
        employee = self.employee_id
        struct = self.struct_id

        # --- Contract / version fields (safely, with fallback if field missing) ---
        contract_vals = {}
        if version:
            # Core contract fields
            contract_vals = {
                "id": version.id,
                "name": version.name if hasattr(version, "name") else False,
                "employee_id": employee.id if employee else False,
                "contract_date_start": str(version.contract_date_start) if hasattr(version, "contract_date_start") and version.contract_date_start else False,
                "contract_date_end": str(version.contract_date_end) if hasattr(version, "contract_date_end") and version.contract_date_end else False,
                "wage": float(version.contract_wage) if hasattr(version, "contract_wage") else False,
                "contract_type": version.contract_type_id.display_name if hasattr(version, "contract_type_id") and version.contract_type_id else False,
                "contract_type_code": version.contract_type_id.code if hasattr(version, "contract_type_id") and version.contract_type_id and hasattr(version.contract_type_id, "code") else False,
                "structure_type": version.structure_type_id.display_name if hasattr(version, "structure_type_id") and version.structure_type_id else False,
                "structure_type_code": version.structure_type_id.default_struct_id.code if hasattr(version, "structure_type_id") and version.structure_type_id and hasattr(version.structure_type_id, "default_struct_id") else False,
                "resource_calendar": version.resource_calendar_id.name if hasattr(version, "resource_calendar_id") and version.resource_calendar_id else False,
                "accounting_type": getattr(version, "accounting_type", False),
                "schedule_pay": getattr(version, "schedule_pay", False),
            }
            # Retention procedure (Sinova field)
            if hasattr(version, "retention_procedure"):
                contract_vals["retention_procedure"] = version.retention_procedure or False
                contract_vals["current_percent"] = getattr(version, "current_percent", False)
            else:
                contract_vals["retention_procedure"] = False
            # Any additional payroll_properties / variables
            try:
                props = dict(version.payroll_properties) if hasattr(version, "payroll_properties") and version.payroll_properties else {}
                contract_vals["payroll_properties"] = props
            except Exception:
                contract_vals["payroll_properties"] = {}
            # Deduction retention ids (procedure 2)
            if hasattr(version, "deduction_retention_ids"):
                try:
                    contract_vals["deduction_retention_count"] = len(version.deduction_retention_ids)
                except Exception:
                    contract_vals["deduction_retention_count"] = 0
            # Generic fallback: dump all fields that look like configuration
            for fname in ["wage_type", "work_time_rate", "hours_per_week"]:
                if hasattr(version, fname):
                    try:
                        contract_vals[fname] = getattr(version, fname)
                    except Exception:
                        pass
            # Contract dates vs payslip dates check
            try:
                if version.contract_date_start and self.date_from and version.contract_date_start > self.date_from:
                    contract_vals["contract_dates_issue"] = f"Contrato inicia {version.contract_date_start} después de inicio de nómina {self.date_from}"
                elif version.contract_date_end and self.date_to and version.contract_date_end < self.date_to:
                    contract_vals["contract_dates_issue"] = f"Contrato termina {version.contract_date_end} antes de fin de nómina {self.date_to}"
            except Exception:
                pass

        # --- Employee info ---
        employee_vals = {}
        if employee:
            employee_vals = {
                "id": employee.id,
                "name": employee.name,
                "department": employee.department_id.name if employee.department_id else False,
                "job": employee.job_id.name if employee.job_id else False,
                "company": self.company_id.name if self.company_id else False,
                "country_code": self.country_code or False,
            }

        # --- Payslip header ---
        payslip_vals = {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "date_from": str(self.date_from) if self.date_from else False,
            "date_to": str(self.date_to) if self.date_to else False,
            "struct": self.struct_id.name if self.struct_id else False,
            "struct_code": self.struct_id.code if self.struct_id and hasattr(self.struct_id, "code") else False,
            "basic_wage": float(self.basic_wage) if self.basic_wage else 0.0,
            "gross_wage": float(self.gross_wage) if self.gross_wage else 0.0,
            "net_wage": float(self.net_wage) if self.net_wage else 0.0,
            "currency": self.currency_id.name if self.currency_id else False,
            "currency_symbol": self.currency_id.symbol if self.currency_id else False,
        }

        # --- Worked days ---
        worked_days = []
        for wd in self.worked_days_line_ids:
            worked_days.append({
                "code": wd.code or wd.work_entry_type_id.code if wd.work_entry_type_id else False,
                "name": wd.name or (wd.work_entry_type_id.name if wd.work_entry_type_id else False),
                "number_of_days": float(wd.number_of_days) if wd.number_of_days else 0.0,
                "number_of_hours": float(wd.number_of_hours) if wd.number_of_hours else 0.0,
                "amount": float(wd.amount) if wd.amount else 0.0,
                "is_paid": bool(wd.is_paid) if hasattr(wd, "is_paid") else True,
            })

        # --- Payslip lines (rules) ---
        lines = []
        for line in self.line_ids:
            lines.append({
                "code": line.code,
                "name": line.name,
                "category_code": line.category_id.code if line.category_id else False,
                "category_name": line.category_id.name if line.category_id else False,
                "quantity": float(line.quantity) if line.quantity else 0.0,
                "rate": float(line.rate) if line.rate else 0.0,
                "amount": float(line.amount) if line.amount else 0.0,
                "total": float(line.total) if line.total is not None else 0.0,
                "appears_on_payslip": bool(line.appears_on_payslip) if hasattr(line, "appears_on_payslip") else True,
            })
        # Also capture zero-value core rules explicitly
        zero_lines = [l for l in lines if l["total"] == 0]

        # --- Inputs ---
        inputs = []
        for inp in self.input_line_ids:
            inputs.append({
                "code": inp.code or (inp.input_type_id.code if inp.input_type_id else False),
                "name": inp.name or (inp.input_type_id.name if inp.input_type_id else False),
                "amount": float(inp.amount) if inp.amount else 0.0,
                "input_type": inp.input_type_id.name if inp.input_type_id else False,
            })

        # --- Contract variables / payslip properties ---
        payslip_properties = {}
        try:
            payslip_properties = dict(self.payslip_properties) if self.payslip_properties else {}
        except Exception:
            payslip_properties = {}

        # --- UVT / thresholds (if annual_data exists) ---
        uvt_info = {}
        try:
            annual_data = self.env["hr.annual.data"].search([("year", "=", self.date_from.year if self.date_from else fields.Date.today().year)], limit=1)
            if annual_data:
                # Try to find UVT value; field names vary per localization
                for fname in ["uvt", "uvt_value", "value_uvt", "amount_uvt"]:
                    if hasattr(annual_data, fname):
                        val = getattr(annual_data, fname)
                        if val:
                            uvt_info["uvt_value"] = float(val)
                            break
                uvt_info["year"] = annual_data.year
        except Exception:
            pass
        if not uvt_info.get("uvt_value"):
            uvt_info["uvt_value"] = float(DEFAULT_UVT_VALUE)
            uvt_info["year"] = str(self.date_from.year) if self.date_from else str(fields.Date.today().year)
            uvt_info["fallback"] = True
        # Common threshold: 95 UVT as per DIAN
        try:
            uvt_info["threshold_95_uvt"] = float(uvt_info["uvt_value"]) * 95
        except Exception:
            uvt_info["threshold_95_uvt"] = float(DEFAULT_UVT_THRESHOLD_PESOS)

        # --- Structure & Rules audit (duplicates, missing accounts, empty lines) ---
        structure_info = {
            "struct_id": struct.id if struct else False,
            "struct_name": struct.name if struct else False,
            "struct_code": struct.code if struct and hasattr(struct, "code") else False,
        }
        structure_rules = []
        duplicates = []
        missing_accounts = []
        try:
            if struct:
                Rule = self.env["hr.salary.rule"]
                # All rules in structure (via struct.rule_ids or directly)
                rules = Rule.search([("struct_id", "=", struct.id)]) if hasattr(Rule, "struct_id") else self.env["hr.salary.rule"].search([])
                # Fallback: try via many2many struct_ids
                if not rules and hasattr(Rule, "_fields") and "struct_ids" in Rule._fields:
                    rules = Rule.search([("struct_ids", "in", struct.id)])
                # If still empty, try via payslip lines codes to infer
                if not rules:
                    # Use codes from payslip lines as proxy
                    rules = Rule.search([("code", "in", [l["code"] for l in lines if l.get("code")])])
                seen_codes = {}
                for r in rules:
                    code = getattr(r, "code", False)
                    # Check accounts
                    has_account = True
                    try:
                        if hasattr(r, "account_line_ids"):
                            has_account = bool(r.account_line_ids)
                        elif hasattr(r, "account_credit") or hasattr(r, "account_debit"):
                            has_account = bool(getattr(r, "account_credit", False) or getattr(r, "account_debit", False))
                    except Exception:
                        has_account = True
                    structure_rules.append({
                        "code": code,
                        "name": getattr(r, "name", False),
                        "active": bool(getattr(r, "active", True)),
                        "has_account": has_account,
                        "appears_on_payslip": bool(getattr(r, "appears_on_payslip", True)),
                    })
                    if code:
                        seen_codes.setdefault(code, []).append(r.id)
                for code, ids in seen_codes.items():
                    if len(ids) > 1:
                        duplicates.append({"code": code, "rule_ids": ids, "count": len(ids)})
                # Missing accounts list
                missing_accounts = [r["code"] for r in structure_rules if not r["has_account"] and r["code"]]
        except Exception as e:
            _logger.debug("Structure audit failed: %s", e)

        # --- Diagnostics flags ---
        diagnostics = {
            "has_lines": bool(lines),
            "lines_count": len(lines),
            "zero_lines_count": len(zero_lines),
            "has_worked_days": bool(worked_days),
            "worked_days_count": len(worked_days),
            "has_inputs": bool(inputs),
            "duplicates": duplicates,
            "missing_accounts": missing_accounts[:10],
            "structure_rules_count": len(structure_rules),
        }
        if contract_vals.get("contract_dates_issue"):
            diagnostics["contract_dates_issue"] = contract_vals["contract_dates_issue"]
        if not contract_vals.get("wage"):
            diagnostics["contract_missing_wage"] = True
        if not contract_vals.get("contract_type"):
            diagnostics["contract_missing_type"] = True
        if not contract_vals.get("resource_calendar"):
            diagnostics["contract_missing_calendar"] = True

        payload = {
            "payslip": payslip_vals,
            "employee": employee_vals,
            "contract": contract_vals,
            "worked_days": worked_days,
            "payslip_lines": lines,
            "zero_value_lines": zero_lines,
            "inputs": inputs,
            "payslip_properties": payslip_properties,
            "uvt_info": uvt_info,
            "structure_info": structure_info,
            "structure_rules": structure_rules[:50],
            "diagnostics": diagnostics,
            "meta": {
                "model": "hr.payslip",
                "audit_version": "2.0",
            },
        }
        return payload

    # -----------------------------------------------------------------------
    # Helpers: system prompt, LLM call, parsing, fallback
    # -----------------------------------------------------------------------
    def _get_sinova_audit_system_prompt(self):
        """Retrieve system prompt from ir.config_parameter (data file) or fallback constant."""
        param = self.env["ir.config_parameter"].sudo().get_param("snva_payroll_ai_audit.system_prompt")
        if param:
            return param
        return SINOVA_AUDIT_SYSTEM_PROMPT

    def _call_llm_audit(self, payload_dict):
        """Call Odoo 19 AI LLM backend. Returns dict or None on failure.

        Uses odoo.addons.ai.utils.llm_api_service.LLMApiService if available.
        Falls back to rule engine if LLM unavailable / no API key.
        """
        try:
            from odoo.addons.ai.utils.llm_api_service import LLMApiService
            from odoo.addons.ai.utils.llm_providers import get_provider
        except Exception as e:
            _logger.info("AI audit: LLM service not available (%s), using fallback", e)
            return None

        # Determine provider / model from ai.agent or config
        # Try to find a configured agent or use default gpt-4o / gpt-4o-mini
        llm_model = "gpt-4o"
        provider = "openai"
        try:
            # Try to get default AI provider from first ai.agent
            Agent = self.env["ai.agent"].sudo().search([], limit=1)
            if Agent:
                llm_model = Agent[0].llm_model or llm_model
                provider = Agent[0]._get_provider() if hasattr(Agent[0], "_get_provider") else provider
            else:
                # Check if provider helper exists, fallback to openai
                provider = get_provider(self.env, llm_model)
        except Exception:
            # Safe fallback
            provider = "openai"

        system_prompt = self._get_sinova_audit_system_prompt()
        # Prepare JSON schema for strict output
        json_schema = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ok", "warning", "error"]},
                "has_issues": {"type": "boolean"},
                "summary": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {"type": "string", "enum": ["warning", "error"]},
                            "rule_code": {"type": "string"},
                            "problem": {"type": "string"},
                            "solution": {"type": "string"},
                        },
                        "required": ["severity", "rule_code", "problem", "solution"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["status", "has_issues", "summary", "findings"],
            "additionalProperties": False,
        }

        user_prompt = (
            "Analyze the following hr.payslip JSON payload and return the audit JSON:\n\n"
            + json.dumps(payload_dict, ensure_ascii=False, indent=2, default=str)
        )

        try:
            service = LLMApiService(env=self.env, provider=provider)
            # Use schema if provider supports it (openai does)
            response_list = service.request_llm(
                llm_model=llm_model,
                system_prompts=[system_prompt],
                user_prompts=[user_prompt],
                schema=json_schema,
                temperature=0.2,
            )
            if not response_list:
                _logger.warning("AI audit: empty LLM response")
                return None
            # request_llm returns list[str]; first entry is the JSON
            raw_text = response_list[0] if isinstance(response_list, list) else str(response_list)
            return self._parse_llm_json_response(raw_text)
        except Exception as e:
            _logger.warning("AI audit LLM call failed: %s", e, exc_info=True)
            return None

    def _parse_llm_json_response(self, raw_text):
        """Extract JSON from LLM response (may be wrapped in markdown)."""
        if not raw_text:
            return None
        text = raw_text.strip()
        # Remove markdown code fences if present
        if text.startswith("```"):
            # Remove ```json ... ```
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()
        # Try direct json load
        try:
            data = json.loads(text)
            return data
        except Exception:
            pass
        # Try extract first {...} block
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                data = json.loads(text[start : end + 1])
                return data
        except Exception as e:
            _logger.warning("AI audit: failed to parse LLM JSON: %s | raw=%s", e, raw_text[:500])
        return None

    def _fallback_rule_audit(self, payload):
        """Deterministic rule-based audit when LLM unavailable.

        Covers all surrounding cases: retention, provisions, zero values, duplicates, missing accounts,
        contract data, worked days, empty lines. All findings are user-configurable unless noted as dev.
        """
        findings = []
        payslip = payload.get("payslip", {})
        contract = payload.get("contract", {})
        lines = payload.get("payslip_lines", []) or payload.get("line_ids", [])
        diagnostics = payload.get("diagnostics", {}) or {}
        structure_info = payload.get("structure_info", {}) or {}
        # Build code -> total map
        code_to_total = {l.get("code"): float(l.get("total") or 0) for l in lines if l.get("code")}
        gross = float(payslip.get("gross_wage") or 0)
        # If gross not computed (maybe line GROSS), try categories
        if not gross:
            gross = float(code_to_total.get("GROSS") or code_to_total.get("BASIC") or 0)
        uvt_info = payload.get("uvt_info", {})
        threshold = float(uvt_info.get("threshold_95_uvt") or DEFAULT_UVT_THRESHOLD_PESOS)

        # Rule 1: RET_FTE
        retention_total = 0
        retention_code_found = None
        for code in RETENTION_CODES:
            if code in code_to_total:
                retention_total = float(code_to_total[code])
                retention_code_found = code
                break
        # If zero but gross > threshold, check procedure
        if retention_total == 0 and gross > threshold:
            procedure = contract.get("retention_procedure")
            if not procedure:
                findings.append({
                    "severity": "warning",
                    "rule_code": retention_code_found or "RET_FTE",
                    "problem": f"Retención en la Fuente calculada en $0 con ingreso bruto ${gross:,.0f} superior al umbral mínimo gravable (95 UVT ≈ ${threshold:,.0f}), pero el contrato no tiene Procedimiento 1 o 2 configurado.",
                    "solution": "Vaya a Empleados → Contratos → pestaña Nómina/Retención y configure 'Procedimiento 1' o 'Procedimiento 2'. Si elige Procedimiento 2, informe el porcentaje vigente. Luego recalcule la nómina.",
                })
            elif procedure == "procedimiento2":
                # Also check current_percent
                pct = contract.get("current_percent")
                try:
                    pct_val = float(pct) if pct not in (False, None, "") else 0
                except Exception:
                    pct_val = 0
                if pct_val <= 0:
                    findings.append({
                        "severity": "error",
                        "rule_code": retention_code_found or "RET_FTE",
                        "problem": "Retención con Procedimiento 2 sin porcentaje vigente configurado.",
                        "solution": "En el contrato, campo 'Porcentaje actual' (current_percent) debe ser > 0 para Procedimiento 2. Empleados → Contratos → Retención.",
                    })

        # Rule 2: Provisions
        provision_inputs = {i.get("code"): i for i in payload.get("inputs", []) if i.get("code")}
        payslip_props = payload.get("payslip_properties", {}) or {}
        for pcode in PROVISION_CODES:
            total = code_to_total.get(pcode)
            if total is not None and float(total) == 0:
                # Check if base variable present in contract or inputs
                # Heuristic: look for payslip_properties that match code, or inputs
                has_base = False
                # If any payslip_property key contains code or is numeric property
                if payslip_props:
                    # Sinova often uses rule ids as keys; check if any value non-zero
                    has_base = any(float(v) != 0 for v in payslip_props.values() if isinstance(v, (int, float)) and v)
                # Also check if any input references base
                if not has_base and provision_inputs:
                    has_base = True  # if any input exists, assume base variable linkage might be ok but still warn
                # For accurate warning: if zero, always flag as likely missing setup
                # Only flag provision codes that are actually in structure? If code exists in lines but zero → flag
                findings.append({
                    "severity": "warning",
                    "rule_code": pcode,
                    "problem": f"La provisión {pcode} se liquidó en $0. Posible variable base no vinculada en contrato o falta insumo en la nómina.",
                    "solution": f"Verifique Nómina → Estructuras → Reglas salariales → {pcode}: que tenga cuenta contable y variable base configurada; y en Empleados → Contratos → Nómina la variable base esté informada o registre el insumo correspondiente en la nómina.",
                })
                # Limit to avoid spam: only first 3 provision warnings
                if len([f for f in findings if f["rule_code"] in PROVISION_CODES]) >= 3:
                    break

        # Rule 3: Zero-value core rules
        # Core rules that should normally be >0 when gross>0
        for code in CORE_RULE_CODES:
            if code in code_to_total and float(code_to_total[code]) == 0:
                # Skip RET_FTE already handled if gross <= threshold (then not an error)
                if code in RETENTION_CODES and gross <= threshold:
                    continue
                # Avoid duplicate with provision handling
                if code in PROVISION_CODES:
                    continue
                # If basic/gross is zero but employee has wage, flag
                if code in ("BASIC", "GROSS") and gross == 0 and code == "GROSS":
                    # Still flag if wage exists
                    contract_wage = contract.get("wage")
                    try:
                        wage_val = float(contract_wage) if contract_wage else 0
                    except Exception:
                        wage_val = 0
                    if wage_val > 0:
                        findings.append({
                            "severity": "error",
                            "rule_code": code,
                            "problem": f"Regla núcleo {code} evaluada en $0 pese a que el contrato tiene salario {wage_val:,.0f}. Probable falta de vinculación de cuentas o parámetros.",
                            "solution": f"Revise Nómina → Estructuras → {code}: condición python, cuentas contables por tipo de contabilización y que el contrato tenga 'Tipo de contabilización' y calendario correctos.",
                        })
                elif code not in ("BASIC", "GROSS", "NET"):
                    findings.append({
                        "severity": "warning",
                        "rule_code": code,
                        "problem": f"Regla {code} evaluada en $0 por falta de parámetros o cuentas no vinculadas.",
                        "solution": f"En Nómina → Reglas salariales busque código {code}, vincule cuentas en pestaña Contabilidad y verifique parámetros (hr.rule.parameter) del periodo.",
                    })

        # --- New checks: duplicates, missing accounts, contract, worked days, empty lines ---
        # Duplicates
        for dup in diagnostics.get("duplicates", []) or []:
            code = dup.get("code")
            findings.append({
                "severity": "error",
                "rule_code": code,
                "problem": f"Existen {dup.get('count')} reglas activas con el mismo código '{code}' en la estructura '{structure_info.get('struct_name')}'. El motor no sabe cuál usar y el cálculo puede ser erróneo.",
                "solution": f"Vaya a Nómina → Configuración → Reglas salariales, busque código {code}, deje solo una activa por estructura y desactive o archive la duplicada. Luego recalcule. (No requiere desarrollo)",
            })
        # Missing accounts
        for code in diagnostics.get("missing_accounts", []) or []:
            if code not in [f["rule_code"] for f in findings]:
                findings.append({
                    "severity": "warning",
                    "rule_code": code,
                    "problem": f"La regla {code} no tiene cuentas contables configuradas para el tipo de contabilización de la nómina.",
                    "solution": f"En Nómina → Reglas salariales → {code} pestaña Contabilidad, vincule las cuentas débito/crédito por empresa. (Configuración, no desarrollo)",
                })
        # Missing contract data
        if diagnostics.get("contract_missing_wage"):
            findings.append({
                "severity": "error",
                "rule_code": "GENERAL",
                "problem": "El contrato no tiene salario informado.",
                "solution": "Empleados → Contratos → Salario: informe el salario base y recalcule.",
            })
        if diagnostics.get("contract_missing_type"):
            findings.append({
                "severity": "warning",
                "rule_code": "GENERAL",
                "problem": "El contrato no tiene tipo de contrato informado.",
                "solution": "Empleados → Contratos → Tipo de contrato: seleccione el tipo.",
            })
        if diagnostics.get("contract_missing_calendar"):
            findings.append({
                "severity": "warning",
                "rule_code": "GENERAL",
                "problem": "El contrato no tiene calendario laboral asignado.",
                "solution": "Empleados → Contratos → Horario de trabajo: asigne el calendario.",
            })
        if diagnostics.get("contract_dates_issue"):
            findings.append({
                "severity": "warning",
                "rule_code": "GENERAL",
                "problem": diagnostics["contract_dates_issue"],
                "solution": "Verifique que las fechas del recibo estén dentro de la vigencia del contrato. Si no, corrija el contrato o el periodo de la nómina.",
            })
        # Worked days
        if not diagnostics.get("has_worked_days"):
            # Only flag if payslip expected worked days and has lines
            if diagnostics.get("lines_count", 0) > 0:
                findings.append({
                    "severity": "warning",
                    "rule_code": "GENERAL",
                    "problem": "La nómina no tiene días trabajados registrados.",
                    "solution": "Revise Nómina → Estructura Tipo y el calendario del contrato, y asegúrese de generar/validar las entradas de trabajo antes de calcular.",
                })
        # Empty lines
        if not diagnostics.get("has_lines") or diagnostics.get("lines_count", 0) == 0:
            findings.append({
                "severity": "error",
                "rule_code": "GENERAL",
                "problem": "El recibo no generó líneas de nómina.",
                "solution": "Verifique que la Estructura Salarial esté asignada al contrato, que las reglas tengan condiciones python/dominio válidas y que no estén filtradas por categoría. Si la fórmula está mal, requiere desarrollo.",
            })
        elif diagnostics.get("lines_count", 0) == 1 and not diagnostics.get("has_worked_days"):
            findings.append({
                "severity": "warning",
                "rule_code": "GENERAL",
                "problem": "El recibo solo generó una línea, posible filtrado incorrecto de reglas.",
                "solution": "Revise las condiciones de las reglas de la estructura y el tipo de estructura. Si es por fórmula, requiere desarrollo.",
            })

        # Deduplicate by rule_code (keep first, but allow GENERAL multiple if different problem)
        seen = set()
        deduped = []
        for f in findings:
            key = (f["rule_code"], f["problem"][:40])
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        findings = deduped
        # Limit to 6 findings to keep message readable
        findings = findings[:6]

        # Determine status
        if any(f["severity"] == "error" for f in findings):
            status = "error"
        elif findings:
            status = "warning"
        else:
            status = "ok"

        summary = ""
        if status == "ok":
            summary = "Auditoría sin hallazgos: la nómina no presenta errores de configuración detectables."
        elif status == "warning":
            summary = f"Se detectaron {len(findings)} posible(s) error(es) de configuración. Ajuste antes de validar; no requiere desarrollo salvo que sea fórmula."
        else:
            summary = f"Se detectaron {len(findings)} error(es) crítico(s). Revise duplicados, cuentas o datos del contrato. Si es fórmula, requiere desarrollo."

        return {
            "status": status,
            "has_issues": bool(findings),
            "summary": summary,
            "findings": findings,
        }

    # -----------------------------------------------------------------------
    # Main action
    # -----------------------------------------------------------------------
    def action_validate_sinova_config(self):
        """Called by UI button 'Validar Configuración (IA)'.

        Sends System Prompt + JSON payload to LLM backend, parses response,
        updates audit fields and logs chatter message if warnings/errors.
        """
        # Ensure single record for payload; allow batch but process individually
        for payslip in self:
            payload = payslip._get_sinova_audit_payload()
            # Try LLM first
            result = payslip._call_llm_audit(payload)
            used_fallback = False
            if not result:
                result = payslip._fallback_rule_audit(payload)
                used_fallback = True

            # Normalize status
            raw_status = (result.get("status") or "ok").strip().lower()
            if raw_status not in ("ok", "warning", "error"):
                raw_status = "ok" if not result.get("has_issues") else "warning"
            # Map to selection values: ok -> ok, warning -> warning, error -> error
            status_map = {"ok": "ok", "warning": "warning", "error": "error"}
            ai_status = status_map.get(raw_status, "warning" if result.get("has_issues") else "ok")
            summary = result.get("summary") or ""
            # Ensure JSON stored is pretty
            try:
                raw_json = json.dumps(result, ensure_ascii=False, indent=2)
            except Exception:
                raw_json = str(result)

            vals = {
                "ai_audit_status": ai_status,
                "ai_audit_summary": summary,
                "ai_audit_json": raw_json,
            }
            payslip.write(vals)

            # Chatter: mensaje simple y sin duplicar JSON completo
            try:
                if ai_status == "ok":
                    if used_fallback:
                        body = (
                            f"<p>✅ <strong>Sin hallazgos</strong> — {summary}</p>"
                            f"<p style='color:#6c757d; font-size:12px;'>Auditoría por reglas locales (sin clave API en Ajustes &gt; IA).</p>"
                        )
                    else:
                        body = f"<p>✅ <strong>Sin hallazgos</strong> — {summary}</p>"
                else:
                    # warning / error: lista corta y solución
                    body_parts = []
                    icon = "⚠️" if ai_status == "warning" else "🚨"
                    body_parts.append(f"<p><strong>{icon} {summary}</strong></p>")
                    findings = result.get("findings", []) or []
                    if findings:
                        body_parts.append("<ul style='margin:8px 0; padding-left:18px;'>")
                        for f in findings[:3]:
                            body_parts.append(f"<li>{f.get('problem','')}<br/><em>Solución: {f.get('solution','')}</em></li>")
                        body_parts.append("</ul>")
                        if len(findings) > 3:
                            body_parts.append(f"<p style='color:#6c757d; font-size:12px;'>+{len(findings)-3} hallazgo(s) más. Ver pestaña Auditoría IA.</p>")
                    if used_fallback:
                        body_parts.append("<p style='color:#6c757d; font-size:12px;'>Auditoría por reglas locales (sin clave API en Ajustes &gt; IA).</p>")
                    body = "".join(body_parts)
                # Evitar duplicado si el último mensaje ya es idéntico
                last_msg = payslip.message_ids[:1].body if payslip.message_ids else ""
                if body not in last_msg:
                    payslip.message_post(body=body, subtype_xmlid="mail.mt_note")
            except Exception as e:
                _logger.warning("Failed to post chatter message for audit: %s", e)

        # Notification with fallback warning
        if self:
            first = self[0]
            has_fallback = any(p.ai_audit_json and "fallback" in (p.ai_audit_json or "").lower() or True for p in self) if False else False
            # Use the used_fallback from last payslip iteration for the notification
            if used_fallback:
                notif_title = _("Auditoría por reglas locales")
                notif_msg = _("Sin clave API de IA configurada. Se usó motor de reglas. Estado: %s. Configure la clave en Ajustes > IA para habilitar IA.") % first.ai_audit_status
                notif_type = "warning"
                notif_sticky = True
            else:
                notif_title = _("Auditoría completada")
                notif_msg = _("Estado: %s") % first.ai_audit_status
                notif_type = "success" if first.ai_audit_status == "ok" else "warning"
                notif_sticky = False
        else:
            notif_title = _("Auditoría completada")
            notif_msg = _("Auditoría completada")
            notif_type = "success"
            notif_sticky = False
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": notif_title,
                "message": notif_msg,
                "type": notif_type,
                "sticky": notif_sticky,
            },
        }

    def action_clear_ai_audit(self):
        """Reset audit fields (useful for re-audit after fixes)."""
        self.write({
            "ai_audit_status": "draft",
            "ai_audit_summary": False,
            "ai_audit_json": False,
        })
        return True
