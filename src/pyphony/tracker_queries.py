"""GraphQL query strings for the Linear issue tracker API."""

from __future__ import annotations

CANDIDATE_ISSUES_QUERY = """
query CandidateIssues($projectSlug: String!, $stateNames: [String!]!, $first: Int!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $stateNames } }
    }
    first: $first
    after: $after
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      assignee { id displayName }
      inverseRelations(first: 100) {
        nodes {
          type
          issue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

ISSUE_STATES_BY_IDS_QUERY = """
query IssueStatesByIds($ids: [ID!]!, $first: Int!, $after: String) {
  issues(
    filter: {
      id: { in: $ids }
    }
    first: $first
    after: $after
  ) {
    nodes {
      id
      state { name }
      labels { nodes { name } }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

ISSUE_UPDATE_STATE_MUTATION = """
mutation IssueUpdateState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) {
    success
    issue { id state { name } }
  }
}
"""

ISSUE_TEAM_QUERY = """
query IssueTeam($issueId: String!) {
  issue(id: $issueId) {
    team { id }
  }
}
"""

WORKFLOW_STATES_QUERY = """
query WorkflowStates($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name }
  }
}
"""

COMMENT_CREATE_MUTATION = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id body }
  }
}
"""

ISSUE_ATTACHMENTS_QUERY = """
query IssueAttachments($issueId: String!) {
  issue(id: $issueId) {
    attachments {
      nodes {
        url
        title
        sourceType
      }
    }
  }
}
"""

ATTACHMENT_CREATE_MUTATION = """
mutation AttachmentCreate($issueId: String!, $url: String!, $title: String!) {
  attachmentCreate(input: { issueId: $issueId, url: $url, title: $title }) {
    success
    attachment {
      id
      url
      title
    }
  }
}
"""

PROJECT_TEAMS_QUERY = """
query ProjectTeams($projectSlug: String!) {
  projects(filter: { slugId: { eq: $projectSlug } }) {
    nodes {
      id
      teams {
        nodes { id name }
      }
    }
  }
}
"""

ISSUE_CREATE_MUTATION = """
mutation IssueCreate($teamId: String!, $title: String!, $description: String, $stateId: String, $projectId: String) {
  issueCreate(input: { teamId: $teamId, title: $title, description: $description, stateId: $stateId, projectId: $projectId }) {
    success
    issue {
      id
      identifier
      title
      url
      state { name }
    }
  }
}
"""

ISSUE_BY_IDENTIFIER_QUERY = """
query IssueByIdentifier($filter: IssueFilter!, $first: Int!) {
  issues(filter: $filter, first: $first) {
    nodes {
      id
      identifier
      title
      description
      state { name }
      url
    }
  }
}
"""

ISSUE_FULL_BY_IDENTIFIER_QUERY = """
query IssueFullByIdentifier($filter: IssueFilter!, $first: Int!) {
  issues(filter: $filter, first: $first) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      assignee { id displayName }
      inverseRelations(first: 100) {
        nodes {
          type
          issue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
  }
}
"""

ISSUE_UPDATE_MUTATION = """
mutation IssueUpdate($issueId: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $issueId, input: $input) {
    success
    issue {
      id
      identifier
      title
      description
      state { name }
      url
    }
  }
}
"""

ISSUE_COMMENTS_QUERY = """
query IssueComments($issueId: String!) {
  issue(id: $issueId) {
    comments(first: 100) {
      nodes {
        id
        body
        createdAt
        user {
          name
        }
      }
    }
  }
}
"""

ISSUE_LABEL_IDS_QUERY = """
query IssueLabelIds($issueId: String!) {
  issue(id: $issueId) {
    labels {
      nodes { id name }
    }
  }
}
"""

TEAM_LABELS_QUERY = """
query TeamLabels($teamId: ID!) {
  issueLabels(filter: { team: { id: { eq: $teamId } } }, first: 250) {
    nodes { id name }
  }
}
"""

ISSUE_LABEL_CREATE_MUTATION = """
mutation IssueLabelCreate($teamId: String!, $name: String!) {
  issueLabelCreate(input: { teamId: $teamId, name: $name }) {
    success
    issueLabel { id name }
  }
}
"""

ISSUES_BY_STATES_QUERY = """
query IssuesByStates($projectSlug: String!, $stateNames: [String!]!, $first: Int!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $stateNames } }
    }
    first: $first
    after: $after
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      assignee { id displayName }
      inverseRelations(first: 100) {
        nodes {
          type
          issue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""
