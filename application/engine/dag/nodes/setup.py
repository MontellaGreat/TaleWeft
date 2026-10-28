"""Real setup steps, executed by the same engine as chapter DAGs."""
import asyncio
from application.engine.dag.models import NodeCategory, NodeMeta, NodeResult, NodeStatus, PromptMode
from application.engine.dag.registry import BaseNode, NodeRegistry


@NodeRegistry.register('setup_step')
class SetupStepNode(BaseNode):
    MAX_OPERATION_SECONDS = 3600
    meta = NodeMeta(node_type='setup_step', display_name='新书设置步骤', category=NodeCategory.WORLD,
                    can_disable=False, is_configurable=False, prompt_mode=PromptMode.TEMPLATE_ONLY,
                    default_timeout_seconds=600, default_max_retries=0)

    def validate_inputs(self, inputs):
        return True

    async def await_execution(self, operation, context):
        # Audits can make several bounded model requests: review, rewrite,
        # deviation recheck, then semantic recheck. They must not share the
        # budget of a single request. Keep a separate finite operation cap.
        context['shared_state']['setup_model_request_timeout_seconds'] = self.get_timeout()
        action = self._config.prompt_variables['action']
        multiple = action in {'generate', 'validate', 'deviation', 'anti_ai', 'commit', 'verify'}
        if not multiple:
            return await super().await_execution(operation, context)
        seconds = min(self.MAX_OPERATION_SECONDS, self.get_timeout() + 30) if action == 'generate' else self.MAX_OPERATION_SECONDS
        try:
            async with asyncio.timeout(seconds) as deadline:
                return await operation
        except TimeoutError:
            # Leave external cancellation to the timeout context's accounting.
            if deadline.expired():
                raise RuntimeError(f'新书 {action} 步骤达到总时限 ({seconds:g}s)；'
                    '当前候选与已完成调用已保留，请点击“继续生成”。') from None
            raise

    async def execute(self, inputs, context):
        guard = context['shared_state'].get('setup_ownership_guard')
        if guard is not None:
            await guard()
        action = self._config.prompt_variables['action']
        state = context['shared_state']
        state['setup_unit'] = self._config.prompt_variables.get('unit', state.get('setup_stage'))
        outputs = await context['services'].run_step(action, state)
        return NodeResult(status=NodeStatus.SUCCESS, outputs=outputs)
