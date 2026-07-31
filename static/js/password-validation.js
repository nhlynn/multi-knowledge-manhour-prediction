/**
 * Shared password validation UI for MHES.
 *
 * Single source of truth for the password-strength rule set (used by
 * Create User, Admin Reset Password, and Forgot Password's Set New
 * Password step) so all three show identical requirements/feedback
 * instead of each maintaining its own copy. Mirrors the rules already
 * enforced server-side by utils/password_policy.py (length, uppercase,
 * lowercase, digit) plus the special-character rule already shown
 * client-side (not yet enforced server-side — see the Real-Time
 * Password Validation work for why).
 *
 * Namespaced under the existing window.MHES global (see
 * templates/base.html) rather than introducing a separate global.
 */
window.MHES = window.MHES || {};

window.MHES.passwordValidation = (function () {
    var RULES = [
        { id: "length", label: "At least 8 characters", test: function (v) { return v.length >= 8; } },
        { id: "upper", label: "Contains an uppercase letter", test: function (v) { return /[A-Z]/.test(v); } },
        { id: "lower", label: "Contains a lowercase letter", test: function (v) { return /[a-z]/.test(v); } },
        { id: "digit", label: "Contains a number", test: function (v) { return /\d/.test(v); } },
        { id: "special", label: "Contains a special character", test: function (v) { return /[^A-Za-z0-9]/.test(v); } },
    ];

    var STRENGTH_LEVELS = [
        // [minRulesPassed, label, badgeClass]
        [5, "Strong", "bg-success"],
        [4, "Good", "bg-primary"],
        [3, "Fair", "bg-warning text-dark"],
        [0, "Weak", "bg-danger"],
    ];

    /** Evaluate a password against every rule. */
    function evaluate(value) {
        var results = RULES.map(function (rule) {
            return { id: rule.id, label: rule.label, passed: rule.test(value) };
        });
        var rulesPassed = results.filter(function (r) { return r.passed; }).length;
        return { results: results, rulesPassed: rulesPassed, allPassed: rulesPassed === RULES.length };
    }

    function strengthFor(rulesPassed) {
        for (var i = 0; i < STRENGTH_LEVELS.length; i++) {
            if (rulesPassed >= STRENGTH_LEVELS[i][0]) {
                return { label: STRENGTH_LEVELS[i][1], badgeClass: STRENGTH_LEVELS[i][2] };
            }
        }
        return null;
    }

    /**
     * Wire up a live requirements checklist + strength badge for a
     * password input.
     *
     * options:
     *   passwordInput: the <input type="password"> element.
     *   checklistItems: { length, upper, lower, digit, special } — each
     *       an element (typically an <li>) whose text/class this
     *       updates. Any rule id omitted is simply not rendered.
     *   strengthBadge: optional element for the Weak/Fair/Good/Strong
     *       badge; hidden (class "d-none") whenever the field is empty.
     *   onChange: optional function(allPassed) called after every
     *       update — e.g. to gate a submit button.
     *
     * Returns { update: fn } — update() re-evaluates immediately
     * (called once automatically on attach).
     */
    function attachChecklist(options) {
        var passwordInput = options.passwordInput;
        var checklistItems = options.checklistItems || {};
        var strengthBadge = options.strengthBadge;
        var onChange = options.onChange || function () {};

        function update() {
            var value = passwordInput.value;
            var evaluation = evaluate(value);

            evaluation.results.forEach(function (r) {
                var el = checklistItems[r.id];
                if (!el) return;
                el.textContent = (r.passed ? "✓ " : "✗ ") + r.label;
                el.className = r.passed ? "text-success" : "text-danger";
            });

            if (strengthBadge) {
                if (value.length === 0) {
                    strengthBadge.textContent = "";
                    strengthBadge.className = "badge d-none";
                } else {
                    var strength = strengthFor(evaluation.rulesPassed);
                    if (strength) {
                        strengthBadge.textContent = strength.label;
                        strengthBadge.className = "badge " + strength.badgeClass;
                    }
                }
            }

            onChange(evaluation.allPassed);
        }

        passwordInput.addEventListener("input", update);
        update();
        return { update: update };
    }

    /**
     * Wire up live Confirm Password match feedback.
     *
     * options:
     *   passwordInput, confirmInput: the two password fields.
     *   hintEl: element to show the match/mismatch message in.
     *   onChange: optional function(matches) — a blank Confirm
     *       Password always reports matches=true (left to the field's
     *       own "required" validation, not treated as a mismatch).
     *
     * Returns { update: fn }.
     */
    function attachConfirmMatch(options) {
        var passwordInput = options.passwordInput;
        var confirmInput = options.confirmInput;
        var hintEl = options.hintEl;
        var onChange = options.onChange || function () {};

        function update() {
            var matches;
            if (!confirmInput.value) {
                hintEl.textContent = "";
                hintEl.className = "form-text";
                matches = true;
            } else if (confirmInput.value === passwordInput.value) {
                hintEl.textContent = "✓ Passwords match.";
                hintEl.className = "form-text text-success";
                matches = true;
            } else {
                hintEl.textContent = "✗ Passwords do not match.";
                hintEl.className = "form-text text-danger";
                matches = false;
            }
            onChange(matches);
        }

        passwordInput.addEventListener("input", update);
        confirmInput.addEventListener("input", update);
        update();
        return { update: update };
    }

    return {
        evaluate: evaluate,
        attachChecklist: attachChecklist,
        attachConfirmMatch: attachConfirmMatch,
    };
})();
