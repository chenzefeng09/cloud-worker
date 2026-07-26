% SPACECRAFT_ATTITUDE_E2E_SMOKE
% MATLAB R2019b-compatible end-to-end aerospace toolbox validation.
%
% This script intentionally creates every final artifact from MATLAB.  It
% exercises Control System Toolbox, Simulink, Stateflow, Simulink Control
% Design, Aerospace Toolbox, and Aerospace Blockset in one repeatable run.

clear;
clc;
bdclose('all');

workspace_dir = '/workspace';
log_path = fullfile(workspace_dir, 'matlab_run.log');
if exist(log_path, 'file') == 2
    delete(log_path);
end
diary(log_path);
cleanup_diary = onCleanup(@() diary('off'));

fprintf('Spacecraft attitude E2E validation started: %s\n', datestr(now, 31));
fprintf('MATLAB: %s\n', version);

%% Environment and toolbox inventory
aerospace_blockset_dir = fullfile(matlabroot, 'toolbox', ...
    'aeroblks', 'aeroblks');
if exist(aerospace_blockset_dir, 'dir') == 7
    addpath(aerospace_blockset_dir);
end

inventory_path = fullfile(workspace_dir, 'toolbox_inventory.txt');
inventory_fid = fopen(inventory_path, 'w');
assert(inventory_fid >= 0, 'Unable to create toolbox inventory');
cleanup_inventory = onCleanup(@() fclose_if_open(inventory_fid));

fprintf(inventory_fid, 'MATLAB spacecraft attitude E2E toolbox inventory\n');
fprintf(inventory_fid, 'Generated: %s\n\n', datestr(now, 31));
fprintf(inventory_fid, '%s\n', evalc('ver'));

[mem_status, mem_text] = system('grep -E "^MemTotal:|^MemAvailable:" /proc/meminfo');
if mem_status == 0
    fprintf(inventory_fid, '\n%s\n', strtrim(mem_text));
end

probe_names = {
    'lqr', 'Control System Toolbox';
    'new_system', 'Simulink';
    'sfroot', 'Stateflow';
    'operspec', 'Simulink Control Design';
    'angle2quat', 'Aerospace Toolbox';
    'quatconj', 'Aerospace Toolbox';
    'quat2dcm', 'Aerospace Toolbox';
    'aerolib', 'Aerospace Blockset'
};
probe_ok = true(size(probe_names, 1), 1);
for probe_index = 1:size(probe_names, 1)
    probe_path = which(probe_names{probe_index, 1});
    probe_ok(probe_index) = ~isempty(probe_path);
    fprintf(inventory_fid, '%-28s %-5s %s\n', ...
        probe_names{probe_index, 1}, pass_fail(probe_ok(probe_index)), probe_path);
end

%% Six-state spacecraft attitude LQR design
J = diag([120, 100, 80]);
A = [zeros(3), eye(3); zeros(3), zeros(3)];
B = [zeros(3); inv(J)];
C = eye(6);
D = zeros(6, 3);
Q = diag([120, 120, 120, 15, 15, 15]);
R = 0.25 * eye(3);

controllability_rank = rank(ctrb(A, B));
[K, ~, closed_loop_poles] = lqr(A, B, Q, R);
Acl = A - B * K;

t = (0:0.05:120)';
x0 = [deg2rad([30; -15; 45]); deg2rad([0.8; -0.5; 0.4])];
closed_loop_system = ss(Acl, zeros(6, 1), eye(6), zeros(6, 1));
[x, tout] = initial(closed_loop_system, x0, t);
u = -(K * x')';

attitude_error_deg = sqrt(sum(rad2deg(x(:, 1:3)).^2, 2));
angular_rate_deg_s = sqrt(sum(rad2deg(x(:, 4:6)).^2, 2));
control_torque_norm = sqrt(sum(u.^2, 2));
settling_time_s = settling_time(tout, attitude_error_deg, angular_rate_deg_s);

lqr_pass = controllability_rank == 6 ...
    && all(real(closed_loop_poles) < 0) ...
    && attitude_error_deg(end) < 0.05 ...
    && angular_rate_deg_s(end) < 0.01 ...
    && isfinite(settling_time_s);

fprintf('LQR rank=%d, final error=%.6f deg, final rate=%.6f deg/s, settle=%.2f s: %s\n', ...
    controllability_rank, attitude_error_deg(end), angular_rate_deg_s(end), ...
    settling_time_s, pass_fail(lqr_pass));

%% Aerospace Toolbox quaternion validation
q = angle2quat(deg2rad(45), deg2rad(-15), deg2rad(30), 'ZYX');
q = quatnormalize(q);
q_conjugate = quatconj(q);
q_identity = quatmultiply(q, q_conjugate);
dcm = quat2dcm(q);
reference_vector = [1, 0, 0];
rotated_vector = quatrotate(q, reference_vector);
roundtrip_vector = quatrotate(q_conjugate, rotated_vector);

quaternion_norm_error = abs(norm(q) - 1);
quaternion_identity_error = norm(q_identity - [1, 0, 0, 0]);
quaternion_roundtrip_error = norm(roundtrip_vector - reference_vector);
dcm_orthogonality_error = norm(dcm * dcm' - eye(3), 'fro');

quaternion_pass = quaternion_norm_error < 1e-12 ...
    && quaternion_identity_error < 1e-10 ...
    && quaternion_roundtrip_error < 1e-10 ...
    && dcm_orthogonality_error < 1e-10;

fprintf('Quaternion norm=%g, identity=%g, roundtrip=%g, DCM=%g: %s\n', ...
    quaternion_norm_error, quaternion_identity_error, ...
    quaternion_roundtrip_error, dcm_orthogonality_error, ...
    pass_fail(quaternion_pass));

%% Simulink state-space model and real simulation
linear_model = 'attitude_linear_model';
linear_model_path = fullfile(workspace_dir, [linear_model '.slx']);
close_if_loaded(linear_model);
delete_if_exists(linear_model_path);

new_system(linear_model);
add_block('simulink/Sources/Constant', [linear_model '/ZeroInput'], ...
    'Value', '0', 'Position', [35, 95, 65, 125]);
add_block('simulink/Continuous/State-Space', [linear_model '/ClosedLoop6State'], ...
    'A', 'Acl', 'B', 'zeros(6,1)', 'C', 'eye(6)', 'D', 'zeros(6,1)', ...
    'X0', 'x0', 'Position', [130, 70, 285, 150]);
add_block('simulink/Sinks/To Workspace', [linear_model '/StateHistory'], ...
    'VariableName', 'sim_state_history', 'SaveFormat', 'Array', ...
    'Position', [350, 90, 455, 130]);
add_line(linear_model, 'ZeroInput/1', 'ClosedLoop6State/1');
add_line(linear_model, 'ClosedLoop6State/1', 'StateHistory/1');
set_param(linear_model, 'StopTime', '120', 'Solver', 'ode45', ...
    'ReturnWorkspaceOutputs', 'on');
save_system(linear_model, linear_model_path);

sim_output = sim(linear_model);
sim_state_history = sim_output.get('sim_state_history');
assert(size(sim_state_history, 2) >= 6, ...
    'Simulink state history contains fewer than six state columns');
sim_state_values = sim_state_history(:, end-5:end);
simulink_final_error_deg = norm(rad2deg(sim_state_values(end, 1:3)));
simulink_final_rate_deg_s = norm(rad2deg(sim_state_values(end, 4:6)));
simulink_pass = size(sim_state_values, 2) == 6 ...
    && simulink_final_error_deg < 0.05 ...
    && simulink_final_rate_deg_s < 0.01 ...
    && file_nonempty(linear_model_path);

fprintf('Simulink samples=%d, final error=%.6f deg, final rate=%.6f deg/s: %s\n', ...
    size(sim_state_history, 1), simulink_final_error_deg, ...
    simulink_final_rate_deg_s, pass_fail(simulink_pass));

%% Simulink Control Design operating specification
try
    operating_spec = operspec(linear_model);
    control_design_pass = ~isempty(operating_spec) ...
        && numel(operating_spec.States) >= 1;
    operating_state_count = numel(operating_spec.States);
catch control_design_error
    operating_spec = [];
    operating_state_count = 0;
    control_design_pass = false;
    fprintf('operspec error: %s\n', control_design_error.message);
end
fprintf('operspec states=%d: %s\n', operating_state_count, ...
    pass_fail(control_design_pass));

%% Stateflow three-mode chart
stateflow_model = 'mode_manager';
stateflow_model_path = fullfile(workspace_dir, [stateflow_model '.slx']);
close_if_loaded(stateflow_model);
delete_if_exists(stateflow_model_path);

new_system(stateflow_model);
close_system(stateflow_model, 0);
sfnew(stateflow_model);
stateflow_root = sfroot;
block_diagram = stateflow_root.find('-isa', 'Simulink.BlockDiagram', ...
    'Name', stateflow_model);
assert(~isempty(block_diagram), 'Unable to locate Stateflow block diagram');
mode_charts = block_diagram(1).find('-isa', 'Stateflow.Chart');
assert(~isempty(mode_charts), 'sfnew did not create a Stateflow chart');
mode_chart = mode_charts(1);
mode_chart.Name = 'ModeManager';

detumble_state = Stateflow.State(mode_chart);
detumble_state.Name = 'DETUMBLE';
detumble_state.LabelString = 'DETUMBLE';
detumble_state.Position = [40, 80, 120, 60];

slew_state = Stateflow.State(mode_chart);
slew_state.Name = 'SLEW';
slew_state.LabelString = 'SLEW';
slew_state.Position = [230, 80, 120, 60];

hold_state = Stateflow.State(mode_chart);
hold_state.Name = 'HOLD';
hold_state.LabelString = 'HOLD';
hold_state.Position = [420, 80, 120, 60];

detumble_to_slew = Stateflow.Transition(mode_chart);
detumble_to_slew.Source = detumble_state;
detumble_to_slew.Destination = slew_state;
detumble_to_slew.LabelString = '[rate_norm < 0.5]';

slew_to_hold = Stateflow.Transition(mode_chart);
slew_to_hold.Source = slew_state;
slew_to_hold.Destination = hold_state;
slew_to_hold.LabelString = '[attitude_error < 2.0]';

save_system(stateflow_model, stateflow_model_path);
close_system(stateflow_model, 0);
load_system(stateflow_model_path);
stateflow_root = sfroot;
reopened_diagram = stateflow_root.find('-isa', 'Simulink.BlockDiagram', ...
    'Name', stateflow_model);
reopened_charts = reopened_diagram(1).find('-isa', 'Stateflow.Chart');
reopened_states = reopened_charts(1).find('-isa', 'Stateflow.State');
reopened_transitions = reopened_charts(1).find('-isa', 'Stateflow.Transition');
stateflow_pass = numel(reopened_states) == 3 ...
    && numel(reopened_transitions) >= 2 ...
    && file_nonempty(stateflow_model_path);
fprintf('Stateflow states=%d, transitions=%d: %s\n', ...
    numel(reopened_states), numel(reopened_transitions), ...
    pass_fail(stateflow_pass));

%% Aerospace Blockset library load and catalog
aerospace_blocks = {};
aerospace_blockset_matches = {};
blockset_library = 'aerolib6dof';
try
    load_system(blockset_library);
    aerospace_blocks = find_system(blockset_library, 'LookUnderMasks', 'all', ...
        'FollowLinks', 'on', 'Type', 'Block');
    aerospace_mask = cellfun(@(block_path) ...
        ~isempty(strfind(lower(block_path), '6dof')) ...
        || ~isempty(strfind(lower(block_path), 'quaternion')), ...
        aerospace_blocks);
    aerospace_blockset_matches = aerospace_blocks(aerospace_mask);
    aerospace_blockset_pass = bdIsLoaded(blockset_library) ...
        && ~isempty(aerospace_blocks);
catch aerospace_blockset_error
    aerospace_blockset_pass = false;
    fprintf('%s error: %s\n', blockset_library, ...
        aerospace_blockset_error.message);
end
fprintf('Aerospace Blockset blocks=%d, 6DOF/quaternion matches=%d: %s\n', ...
    numel(aerospace_blocks), ...
    numel(aerospace_blockset_matches), pass_fail(aerospace_blockset_pass));

fprintf(inventory_fid, '\nAerospace Blockset %s load: %s\n', ...
    blockset_library, ...
    pass_fail(aerospace_blockset_pass));
fprintf(inventory_fid, '6DOF/Quaternion matches: %d\n', ...
    numel(aerospace_blockset_matches));
for block_index = 1:min(numel(aerospace_blockset_matches), 30)
    fprintf(inventory_fid, '  %s\n', aerospace_blockset_matches{block_index});
end
%% Mode timeline and MATLAB-generated dashboard
mode_timeline = ones(size(tout));
mode_timeline(attitude_error_deg < 10) = 2;
mode_timeline(attitude_error_deg < 2 & angular_rate_deg_s < 0.1) = 3;

dashboard = figure('Visible', 'off', 'Color', 'w', ...
    'Position', [80, 80, 1280, 850]);
subplot(2, 2, 1);
plot(tout, rad2deg(x(:, 1:3)), 'LineWidth', 1.2);
grid on;
xlabel('Time (s)');
ylabel('Attitude error (deg)');
title('Three-axis attitude error');
legend('Roll', 'Pitch', 'Yaw', 'Location', 'northeast');

subplot(2, 2, 2);
plot(tout, rad2deg(x(:, 4:6)), 'LineWidth', 1.2);
grid on;
xlabel('Time (s)');
ylabel('Angular rate (deg/s)');
title('Body angular rates');
legend('\omega_x', '\omega_y', '\omega_z', 'Location', 'northeast');

subplot(2, 2, 3);
plot(tout, u, 'LineWidth', 1.2);
grid on;
xlabel('Time (s)');
ylabel('Torque (N m)');
title('LQR control torque');
legend('\tau_x', '\tau_y', '\tau_z', 'Location', 'northeast');

subplot(2, 2, 4);
stairs(tout, mode_timeline, 'LineWidth', 1.5);
grid on;
xlabel('Time (s)');
ylabel('Mode');
yticks([1, 2, 3]);
yticklabels({'DETUMBLE', 'SLEW', 'HOLD'});
ylim([0.7, 3.3]);
title('Guidance mode timeline');

dashboard_path = fullfile(workspace_dir, 'attitude_dashboard.png');
print(dashboard, dashboard_path, '-dpng', '-r140');
close(dashboard);
dashboard_pass = file_nonempty(dashboard_path, 10000);

%% Results, report, and JSON validation summary
results_path = fullfile(workspace_dir, 'spacecraft_attitude_results.mat');
save(results_path, 'J', 'A', 'B', 'K', 'Acl', 'closed_loop_poles', ...
    'controllability_rank', 'tout', 'x', 'u', 'attitude_error_deg', ...
    'angular_rate_deg_s', 'control_torque_norm', 'settling_time_s', ...
    'q', 'q_conjugate', 'q_identity', 'dcm', 'mode_timeline', ...
    'sim_state_history', 'sim_state_values', 'simulink_final_error_deg', ...
    'simulink_final_rate_deg_s');

results_pass = file_nonempty(results_path, 1000);
toolbox_probe_pass = all(probe_ok);
overall_pass = lqr_pass && quaternion_pass && simulink_pass ...
    && control_design_pass && stateflow_pass ...
    && aerospace_blockset_pass && dashboard_pass && results_pass ...
    && toolbox_probe_pass;

validation = struct();
validation.overall_pass = overall_pass;
validation.matlab_version = version;
validation.checks = struct( ...
    'toolbox_probes', toolbox_probe_pass, ...
    'lqr', lqr_pass, ...
    'aerospace_quaternion', quaternion_pass, ...
    'simulink', simulink_pass, ...
    'simulink_control_design', control_design_pass, ...
    'stateflow', stateflow_pass, ...
    'aerospace_blockset', aerospace_blockset_pass, ...
    'dashboard', dashboard_pass, ...
    'results_mat', results_pass);
validation.metrics = struct( ...
    'controllability_rank', controllability_rank, ...
    'final_attitude_error_deg', attitude_error_deg(end), ...
    'final_angular_rate_deg_s', angular_rate_deg_s(end), ...
    'settling_time_s', settling_time_s, ...
    'max_control_torque_nm', max(control_torque_norm), ...
    'quaternion_identity_error', quaternion_identity_error, ...
    'simulink_final_error_deg', simulink_final_error_deg, ...
    'stateflow_state_count', numel(reopened_states), ...
    'stateflow_transition_count', numel(reopened_transitions), ...
    'aerolib_match_count', numel(aerospace_blockset_matches));
validation.artifacts = { ...
    'spacecraft_attitude_e2e.m', ...
    'attitude_linear_model.slx', ...
    'mode_manager.slx', ...
    'spacecraft_attitude_results.mat', ...
    'attitude_dashboard.png', ...
    'aerospace_e2e_report.md', ...
    'validation.json', ...
    'matlab_run.log', ...
    'toolbox_inventory.txt'};

validation_path = fullfile(workspace_dir, 'validation.json');
validation_fid = fopen(validation_path, 'w');
assert(validation_fid >= 0, 'Unable to create validation.json');
fprintf(validation_fid, '%s\n', jsonencode(validation));
fclose(validation_fid);

report_path = fullfile(workspace_dir, 'aerospace_e2e_report.md');
report_fid = fopen(report_path, 'w');
assert(report_fid >= 0, 'Unable to create report');
fprintf(report_fid, '# 航天器三轴姿态控制端到端验证报告\n\n');
fprintf(report_fid, '- MATLAB：`%s`\n', version);
fprintf(report_fid, '- 执行时间：`%s`\n', datestr(now, 31));
fprintf(report_fid, '- 总体结果：**%s**\n\n', pass_fail(overall_pass));
fprintf(report_fid, '## 数值指标\n\n');
fprintf(report_fid, '| 指标 | 数值 | 判定 |\n');
fprintf(report_fid, '|---|---:|:---:|\n');
fprintf(report_fid, '| 可控性秩 | %d / 6 | %s |\n', ...
    controllability_rank, pass_fail(controllability_rank == 6));
fprintf(report_fid, '| 最终姿态误差 | %.6f deg | %s |\n', ...
    attitude_error_deg(end), pass_fail(attitude_error_deg(end) < 0.05));
fprintf(report_fid, '| 最终角速度 | %.6f deg/s | %s |\n', ...
    angular_rate_deg_s(end), pass_fail(angular_rate_deg_s(end) < 0.01));
fprintf(report_fid, '| 进入并保持阈值时间 | %.2f s | %s |\n', ...
    settling_time_s, pass_fail(isfinite(settling_time_s)));
fprintf(report_fid, '| 四元数单位乘积误差 | %.3e | %s |\n', ...
    quaternion_identity_error, pass_fail(quaternion_pass));
fprintf(report_fid, '| Simulink 最终姿态误差 | %.6f deg | %s |\n', ...
    simulink_final_error_deg, pass_fail(simulink_pass));
fprintf(report_fid, '| Stateflow 状态/迁移 | %d / %d | %s |\n', ...
    numel(reopened_states), numel(reopened_transitions), ...
    pass_fail(stateflow_pass));
fprintf(report_fid, '| aerolib 6DOF/Quaternion 匹配 | %d | %s |\n', ...
    numel(aerospace_blockset_matches), pass_fail(aerospace_blockset_pass));
fprintf(report_fid, '\n## 工具链验证\n\n');
fprintf(report_fid, '- Control System Toolbox / LQR：**%s**\n', pass_fail(lqr_pass));
fprintf(report_fid, '- Aerospace Toolbox / Quaternion：**%s**\n', pass_fail(quaternion_pass));
fprintf(report_fid, '- Simulink / State-Space sim：**%s**\n', pass_fail(simulink_pass));
fprintf(report_fid, '- Simulink Control Design / operspec：**%s**\n', pass_fail(control_design_pass));
fprintf(report_fid, '- Stateflow / 3-state chart：**%s**\n', pass_fail(stateflow_pass));
fprintf(report_fid, '- Aerospace Blockset / aerolib：**%s**\n', pass_fail(aerospace_blockset_pass));
fprintf(report_fid, '\n## 产物\n\n');
for artifact_index = 1:numel(validation.artifacts)
    fprintf(report_fid, '- `%s`\n', validation.artifacts{artifact_index});
end
fclose(report_fid);

fprintf('OVERALL: %s\n', pass_fail(overall_pass));
fprintf('Artifacts written to %s\n', workspace_dir);
diary('off');

assert(overall_pass, 'One or more aerospace E2E checks failed; inspect validation.json');

%% Local helpers
function value = pass_fail(condition)
    if condition
        value = 'PASS';
    else
        value = 'FAIL';
    end
end

function settle = settling_time(t, attitude_error_deg, rate_deg_s)
    inside = attitude_error_deg < 2 & rate_deg_s < 0.1;
    settle = NaN;
    for idx = 1:numel(t)
        if inside(idx) && all(inside(idx:end))
            settle = t(idx);
            return;
        end
    end
end

function close_if_loaded(model_name)
    if bdIsLoaded(model_name)
        close_system(model_name, 0);
    end
end

function delete_if_exists(path_name)
    if exist(path_name, 'file') == 2
        delete(path_name);
    end
end

function fclose_if_open(file_id)
    if file_id >= 0
        fclose(file_id);
    end
end

function present = file_nonempty(path_name, minimum_bytes)
    if nargin < 2
        minimum_bytes = 0;
    end
    listing = dir(path_name);
    present = ~isempty(listing) && listing(1).bytes > minimum_bytes;
end
